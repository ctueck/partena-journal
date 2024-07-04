#!/usr/bin/env python3
#
# read PDF payroll journal (filtered to text) and extract values to CSV file
#

from flask import Flask, request, jsonify, send_file

from flask_cors import CORS

import os
import io
import json

import pdftotext

import sys
import re
import datetime
import csv

from decimal import Decimal

class Calendar:
    """
    calendar of worked and non-worked days for one staff member in a specific month
    """

    CODES = {
        '001':      None,   # worked
        '002':      None,   # worked
        '002-08':   None,   # worked (from home)
        '006':      'PH',   # public holiday
        '006-49':   None,   # public holiday worked
        '007':      'AH',   # extra holiday
        '011-04':   'AH',   # extra holiday
        '013':      'SL',   # sick leave
        '013-23':   'SL',   # sick leave
        '016':      'AH',   # legal holidays
        '135':      'OA',   # family reasons
        '158':      'AH'    # unpaid leave
    }

    def __init__(self, year, month, payslip, errors):
        self.year = int(year)
        self.month = int(month)
        self.payslip = payslip
        self.data = {}
        self.errors = errors
        i = 0
        while True:
            i += 1
            try:
                day = datetime.date(self.year, self.month, i)
            except(ValueError):
                break;
            else:
                if day.weekday() < 5:
                    self.data[i] = None # self.CODES['001']
                else:
                    self.data[i] = 'WE'

    def set(self, code, d_from, d_to):
        if d_to is None:
            if self.data[int(d_from)] is None:
                try:
                    self.data[int(d_from)] = self.CODES[code]
                except(KeyError):
                    self.errors.append(f"{self.payslip.id} {self.payslip.name} {self.year}/{self.month:02d}: could not map '{code}' to calendar")
        else:
            for i in range(int(d_from), int(d_to) + 1):
                if self.data[i] is None:
                    try:
                        self.data[i] = self.CODES[code]
                    except(KeyError):
                        self.errors.append(f"{self.payslip.id} {self.payslip.name} {self.year}/{self.month:02d}: could not map '{code}' to calendar")

class Payslip:
    """
    holding one staff member's aggregated amounts for a specific month
    """

    FIELDS = {
        'gross':    r'^0[0-9]{2}',          # gross salary
        'onss':     r'^(58[013]|577)',      # employer's part social security
        'lunch':    r'^984',                # employer's part lunch vouchers
        'group':    r'^902',                # group insurance premium
        'travel':   r'^85[01]-[0-9]{2}',    # home-to-work travel costs
        'costs':    r'^857-[0-9]{2}',       # other costs reimbursed
        'yearly':   r'^3[0-9]{2}'           # yearly costs such as year-end premium, double holiday pay, ...
    }
    FIELDS_IGNORE = [
        r'^135',
        r'^2[2348]',
        r'^477',
        r'^57',
        r'^59',
        r'^800',
        r'^814',
        r'^83',
        r'^94',
        r'^98',
    ]
    FIELDS_HOURS = r'^00[12]'

    def __init__(self, id, name, year, month, errors):
        self.id = id
        self.name = name
        self.year = year
        self.month = month
        self.hours = Decimal()
        self.data = { i:Decimal() for i in self.FIELDS }
        self.calendar = Calendar(year, month, self, errors)
        self.errors = errors

    def read(self, match, sign=Decimal('1')):
        """
        read a journal line
        """
        # convert amount to decimal
        if re.match(r'\s*[0-9.,-]+\s*', match['value']):
            amount = Decimal(match['value'].replace(',','.')) * sign
        else:
            amount = Decimal(0)
            if match['value']:
                self.errors.append(f"{self.id} {self.name} {self.year}/{self.month}: value '{match['value']}' for code '{match['code']}' could not be parsed")
        # check code against mapped and ignored regexps
        found = False
        for (field, regexp) in self.FIELDS.items():
            if re.match(regexp, match['code']):
                found = True
                self.data[field] += amount
        for regexp in self.FIELDS_IGNORE:
            if re.match(regexp, match['code']):
                found = True
        if not found:
            self.errors.append(f"{self.id} {self.name} {self.year}/{self.month}: code '{match['code']}' neither handled nor explicitly ignored")
        # set calendar days where applicable
        if match['from']:
            self.calendar.set(match['code'], match['from'], match['to'])
        # sum up hours
        if match['hours'] and re.match(self.FIELDS_HOURS, match['code']):
            parts = match['hours'].split(',')
            self.hours += Decimal(f"{parts[0]}.{int(int(parts[1])*1000/60)}") * sign

    @classmethod
    def fieldnames(cls):
        fields = [ 'id', 'name', 'year', 'month', 'hours' ]
        fields.extend(cls.FIELDS.keys())
        fields.extend(range(1,32))
        return(fields)

    def serialise(self):
        return({
            'id': self.id,
            'name': self.name,
            'year': self.year,
            'month': self.month,
            'hours': self.hours,
            **self.data,
            **self.calendar.data,
        })

class MonthDict(dict):
    """
    dict holding one staff member's monthly payslips
    """

    def __init__(self, id, name, year, errors, **kwargs):
        self.id = id
        self.name = name
        self.year = year
        self.errors = errors
        super().__init__(**kwargs)

    def __missing__(self, key):
        self[key] = Payslip(self.id, self.name, self.year, key, self.errors)
        return(self[key])

class YearDict(dict):
    """
    dict holding one staff member's payslips for one calendar year
    """

    def __init__(self, id, name, errors, **kwargs):
        self.id = id
        self.name = name
        self.errors = errors
        super().__init__(**kwargs)

    def __missing__(self, key):
        self[key] = MonthDict(self.id, self.name, key, self.errors)
        return(self[key])

class Staff(dict):
    """
    dict holding one entry per staff member
    """

    def __init__(self, errors, **kwargs):
        self.errors = errors
        super().__init__(**kwargs)

    def upsert(self, id, name):
        if id not in self:
            self[id] = dict(
                id=id,
                name=name,
                payslips=YearDict(id, name, self.errors)
            )

class PayrollData:
    """
    parses and saves payroll data
    """

    RE_HDR_MONTH = r"^\s*N[°r]\s+[0-9 -]+\s+(du|van)\s+(?P<day>[0-9]+)/(?P<month>[0-9]+)/(?P<year>[0-9]+)"
    RE_HDR_STAFF = r"^│(?P<id>[0-9]{6}) (?P<name>\w+)"
    RE_HDR_SECTION = r"^│\s*│\s*$"
    RE_HDR_NEGATIVE = r"^│\s*NEGATI[E]F"
    RE_DATA_LINE = r"^│[VZ ]│\s*(?P<code>[0-9]{3}(\-[0-9]{2})?)(\s(?P<days>[0-9-]+))?\s*│\s*(?P<hours>[C0-9,-]*)\s*│\s*(?P<from>[0-9]*)(\s*-\s*(?P<to>[0-9]+))?\s*│\s*(?P<unit>[0-9,-]*)\s*│(\s*[0-9,-]*\s*│){3}\s*(?P<value>[0-9,-]*)\s*│\s*(?P<gross>[0-9,-]*)\s*│"

    def __init__(self):

        class ParserState:
            """
            holds the current state of the parser
            """
            def __init__(self):
                self.year = None
                self.month = None
                self.staff = None
                self.sign = Decimal('1')

        self.ignored = list()
        self.errors = list()
        self.debug = list()
        self.data = Staff(self.errors)
        self.current = ParserState()

    def parse(self, line):
        """
        parse lines resulting from pdftotext
        """
        # check if month header
        if match := re.match(self.RE_HDR_MONTH, line):
            if match['year'] != self.current.year:
                self.current.year = match['year']
            if match['month'] != self.current.month:
                self.current.month = match['month']
                self.debug.append(f'HDR : month {self.current.year}/{self.current.month}')
        # check if staff member
        elif match := re.match(self.RE_HDR_STAFF, line):
            self.data.upsert(match['id'], match['name'])
            self.current.staff = match['id']
            self.current.sign = Decimal('1')
            self.debug.append(f'HDR : staff id={self.current.staff} name={match["name"]}')
        # new section - reset sign to positive
        elif re.match(self.RE_HDR_SECTION, line):
            self.current.sign = Decimal('1')
            self.debug.append(f'+++ : empty line - reset to positive')
        # change sign to negative
        elif re.match(self.RE_HDR_NEGATIVE, line):
            self.current.sign = Decimal('-1')
            self.debug.append(f'--- : start of negative section')
        # parse data line
        elif match := re.match(self.RE_DATA_LINE, line):
            if self.current.staff:
                self.data[self.current.staff]['payslips'][self.current.year][self.current.month].read(match, self.current.sign)
                self.debug.append('DATA: code={code} value={value} (days={days} hours={hours} from={from} to={to} unit={unit})'.format(**match.groupdict()))
            else:
                self.errors.append(f"[ERROR: data line before staff header] {line}")
                return False
        else:
            self.ignored.append(line)
            return False
        # indicate that line was parsed in some way
        return True


    def serialize_to_csv(self):
        handle = io.StringIO(newline='')
        writer = csv.DictWriter(handle, fieldnames=Payslip.fieldnames())
        writer.writeheader()

        for staff in sorted(self.data.keys()):
            for year in sorted(self.data[staff]['payslips'].keys()):
                for month in sorted(self.data[staff]['payslips'][year].keys()):
                    writer.writerow(self.data[staff]['payslips'][year][month].serialise())

        return handle.getvalue()


app = Flask(__name__)
CORS(app)

@app.route('/convert', methods=['POST'])
def convert():
    if 'pdf' not in request.files:
        return {'errors': ['No file part in the request']}, 400

    files = request.files.getlist('pdf')

    data = PayrollData()

    # Mock processing, replace this with actual logic to handle the PDFs
    for file in files:
        if file.mimetype != 'application/pdf':
            data.errors.append(f"'{file.filename}' is not a PDF, but has MIME type '{file.mimetype}'")
            continue
        pdf = pdftotext.PDF(file, raw=1)
        parsed = False
        for page in pdf:
            for line in page.split('\n'):
                parsed = data.parse(line) or parsed
        if not parsed:
            data.errors.append(f"'{file.filename}' yielded no rows that could be parsed")

    return {
        'csv': data.serialize_to_csv(),
        'errors': data.errors,
        'ignored': data.ignored,
        'debug': data.debug,
    }

if __name__ == '__main__':
    app.run(debug=True)

