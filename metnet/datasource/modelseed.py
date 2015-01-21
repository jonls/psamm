
'''Module related to loading ModelSEED database files'''

import csv
import re
from decimal import Decimal

from metnet.reaction import Reaction, Compound


class ParseError(Exception):
    '''Exception used to signal errors while parsing'''
    pass


def decode_name(s):
    '''Decode names in ModelSEED files'''
    # Some names contain XML-like entity codes
    return re.sub(r'&#(\d+);', lambda x: chr(int(x.group(1))), s)

class CompoundEntry(object):
    '''Representation of entry in a ModelSEED compound table'''

    def __init__(self, id, names, formula):
        self._id = id
        self._names = list(names)
        self._formula = formula

        # Find shortest name
        # Usually the best name is the shortest one,
        # but in the case of ties, the best one is
        # usually the last one to appear in the list.
        # Since the sort is stable we can obtain this
        # by first reversing the list, then sorting
        # by length.
        self._name = None
        if len(self._names) > 0:
            self._name = sorted(reversed(self._names), key=lambda x: len(x))[0]

    @property
    def id(self):
        return self._id

    @property
    def name(self):
        return self._name

    @property
    def names(self):
        return iter(self._names)

    @property
    def formula(self):
        return self._formula


def parse_compound_file(f):
    '''Iterate over the compound entries in the given file'''

    f.readline() # Skip header
    for row in csv.reader(f, delimiter='\t'):
        compound_id, names, formula = row[:3]
        names = (decode_name(name) for name in names.split(',<br>'))

        # ModelSEED sometimes uses an asterisk and number at
        # the end of formulas. This seems to have a similar
        # meaning as '(...)n'.
        m = re.match(r'^(.*)\*(\d*)$', formula)
        if m is not None:
            if m.group(2) != '':
                formula = '({}){}'.format(m.group(1), m.group(2))
            else:
                formula = '({})n'.format(m.group(1))

        formula = formula.strip()
        if formula == '' or formula == 'noformula':
            formula = None

        yield CompoundEntry(compound_id, names, formula)


def parse_reaction(s):
    """Parse a ModelSEED reaction

    This parser is based on the grammer.::

        <reaction>     ::= <comp-list> ' ' <reaction-dir> ' ' <comp-list>
        <reaction-dir> ::= '<=' | '<=>' | '=>' | '?' | ''
        <comp-list>    ::= '' | <compound> | <compound> ' + ' <comp-list>
        <compound>     ::= <comp-count> ' ' <comp-spec> | <comp-spec>
        <comp-count>   ::= '(' <comp-number> ')' | <comp-number>
        <comp-number>  ::= <decimal>
        <comp-spec>    ::= '|' <comp-id> '|' | 'cdp' <cdp-id>
        <comp-id>      ::= <comp-name> '[' <comp-compart> ']' | <comp-name>
        <comp-compart> ::= <alpha>
        <comp-name>    ::= <any characters other than "|">
        <cdp-id>       ::= <five digits>   ; [sic]
    """

    def tokenize(s):
        """Return tokens of reaction string"""
        s = s.lstrip()
        token = ''
        barquote = False
        for c in s:
            if c.isspace() and not barquote:
                yield token
                token = ''
                continue

            token += c

            if c == '|':
                barquote = not barquote

        if token != '':
            yield token

    def parse_compound_number(number):
        """Parse compound number

        Return plain int if possible, otherwise use Decimal."""

        d = Decimal(number)
        if d % 1 == 0:
            return int(d)
        return d

    def parse_compound_count(count):
        """Parse compound count"""

        m = re.match(r'^\((.+)\)|(.+)$', count)
        if not m:
            raise ParseError('Unable to parse compound count: {}'.format(count))

        number = m.group(1) if m.group(1) is not None else m.group(2)
        return parse_compound_number(number)

    def parse_compound_name(name):
        """Parse compound name"""

        m = re.match(r'^\|(.+)\||(cdp\d+)$', name)
        if not m:
            raise ParseError('Unable to parse compound name: {}'.format(name))

        return m.group(1) if m.group(1) is not None else m.group(2)

    def parse_compound(cmpd):
        """Parse compound"""

        count = 1
        if len(cmpd) == 2:
            count = parse_compound_count(cmpd[0])
            name = parse_compound_name(cmpd[1])
        elif len(cmpd) == 1:
            name = parse_compound_name(cmpd[0])
        else:
            raise ParseError('Unexpected number of tokens in compound: {}'.format(cmpd))

        compartment = None
        m = re.match(r'^(.+?)\[(.+)\]$', name)
        if m is not None:
            name = m.group(1)
            compartment = m.group(2)

        return Compound(name, compartment=compartment), count

    def parse_compound_list(cmpds):
        """Parse a list of compounds"""

        if len(cmpds) == 0:
            return

        cmpd = []
        for t in cmpds:
            if t == '+':
                yield parse_compound(cmpd)
                cmpd = []
            else:
                cmpd.append(t)

        if len(cmpd) == 0:
            raise ParseError('Expected compound in compound list')

        yield parse_compound(cmpd)

    tokens = list(tokenize(s))
    direction = None
    for i, t in enumerate(tokens):
        if t in ('<=', '<=>', '=>', '?', ''):
            direction = t
            left = tokens[:i]
            right = tokens[i+1:]

    if direction is None:
        raise ParseError('Failed to parse reaction: {}'.format(tokens))

    if direction in ('', '?'):
        direction = Reaction.Bidir

    return Reaction(direction, parse_compound_list(left),
                    parse_compound_list(right))

def format_reaction(reaction):
    """Format reaction as string

    converting parsed reactions
    back to string represetation using this module only a subset of the grammar
    is used.::

        <reaction>     ::= <comp-list> ' ' <reaction-dir> ' ' <comp-list>
        <reaction-dir> ::= '<=>' | '=>' | '?'
        <comp-list>    ::= '' | <compound> | <compound> ' + ' <comp-list>
        <compound>     ::= <comp-count> ' ' <comp-spec> | <comp-spec>
        <comp-count>   ::= '(' <decimal> ')'
        <comp-spec>    ::= '|' <comp-name> '|' | '|' <comp-name> '[' <comp-compart> ']' '|'
        <comp-compart> ::= <alpha>
        <comp-name>    ::= <any characters other than "|">
    """

    # Define helper functions
    def format_compound(compound, count):
        """Format compound"""
        cpdspec = str(compound)
        if count != 1:
            return '({}) |{}|'.format(count, cpdspec)
        return '|{}|'.format(cpdspec)

    def format_compound_list(cmpds):
        """Format compound list"""
        return ' + '.join(format_compound(compound, count) for compound, count in cmpds)

    return '{} {} {}'.format(format_compound_list(reaction.left),
                             '?' if reaction.direction == '' else reaction.direction,
                             format_compound_list(reaction.right))
