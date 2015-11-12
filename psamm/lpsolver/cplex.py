# This file is part of PSAMM.
#
# PSAMM is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PSAMM is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PSAMM.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2014-2015  Jon Lund Steffensen <jon_steffensen@uri.edu>

"""Linear programming solver using Cplex."""

from __future__ import absolute_import

import logging
from itertools import repeat, count
import numbers

from six.moves import zip
import cplex as cp

from .lp import Solver as BaseSolver
from .lp import Constraint as BaseConstraint
from .lp import Problem as BaseProblem
from .lp import Result as BaseResult
from .lp import (Expression, Relation, ObjectiveSense, VariableType,
                 InvalidResultError)
from ..util import LoggerFile

# Module-level logging
logger = logging.getLogger(__name__)

_INF = float('inf')


class Solver(BaseSolver):
    """Represents an LP-solver using Cplex"""

    def create_problem(self, **kwargs):
        """Create a new LP-problem using the solver"""
        return Problem(**kwargs)


class Problem(BaseProblem):
    """Represents an LP-problem of a cplex.Solver"""

    VARTYPE_MAP = {
        VariableType.Continuous: 'C',
        VariableType.Binary: 'B',
        VariableType.Integer: 'I'
    }

    CONSTR_SENSE_MAP = {
        Relation.Equals: 'E',
        Relation.Greater: 'G',
        Relation.Less: 'L'
    }

    def __init__(self, **kwargs):
        self._cp = cp.Cplex()

        # Set up output to go to logging streams
        log_stream = LoggerFile(logger, logging.DEBUG)
        warning_stream = LoggerFile(logger, logging.WARNING)
        error_stream = LoggerFile(logger, logging.ERROR)

        self._cp.set_log_stream(log_stream)
        self._cp.set_results_stream(log_stream)
        self._cp.set_warning_stream(warning_stream)
        self._cp.set_error_stream(error_stream)

        # Set feasibility tolerance. By default, we decrease it to 1e-9.
        feasibility_tolerance = kwargs.get('feasibility_tolerance', 1e-9)
        logger.info('Setting feasibility tolerance to {!r}'.format(
            feasibility_tolerance))
        self._cp.parameters.simplex.tolerances.feasibility.set(
            feasibility_tolerance)

        # Set number of threads
        if 'threads' in kwargs:
            logger.info('Setting threads to {!r}'.format(kwargs['threads']))
            self._cp.parameters.threads.set(kwargs['threads'])

        self._cp.parameters.emphasis.numerical.set(True)

        self._variables = {}
        self._var_names = (i for i in count(0))
        self._constr_names = ('c'+str(i) for i in count(1))

        # Keep track of the objective variables that are non-zero
        self._non_zero_objective = set()

        self._result = None

    @property
    def cplex(self):
        """The underlying Cplex object"""
        return self._cp

    def define(self, *names, **kwargs):
        """Define variable in the problem.

        Variables must be defined before they can be accessed by var() or
        set(). This function takes keyword arguments lower and upper to define
        the bounds of the variable (default: -inf to inf). The keyword argument
        types can be used to select the type of the variable (Continuous
        (default), Binary or Integer). Setting any variables different than
        Continuous will turn the problem into an MILP problem.
        """
        names = tuple(names)
        lower = kwargs.get('lower', None)
        upper = kwargs.get('upper', None)
        vartype = kwargs.get('types', None)

        # Repeat values if a scalar is given
        if lower is None or isinstance(lower, numbers.Number):
            lower = repeat(lower, len(names))
        if upper is None or isinstance(upper, numbers.Number):
            upper = repeat(upper, len(names))
        if vartype is None or vartype in (
                VariableType.Continuous, VariableType.Binary,
                VariableType.Integer):
            vartype = repeat(vartype, len(names))

        lp_names = tuple(next(self._var_names) for name in names)

        # Assign default values
        lower = (-cp.infinity if value is None or value == -_INF
                 else value for value in lower)
        upper = (cp.infinity if value is None or value == _INF
                 else value for value in upper)
        vartype = tuple(VariableType.Continuous if value is None else value
                        for value in vartype)

        args = {'lb': tuple(lower), 'ub': tuple(upper)}
        if any(value != VariableType.Continuous for value in vartype):
            # Set types only if some are integer (otherwise Cplex will change
            # the solver to MILP).
            args['types'] = tuple(Problem.VARTYPE_MAP[t] for t in vartype)

        self._variables.update(zip(names, lp_names))
        self._cp.variables.add(**args)

    def has_variable(self, name):
        """Check whether variable is defined in the model."""
        return name in self._variables

    def _add_constraints(self, relation):
        """Add the given relation as one or more constraints

        Return a list of the names of the constraints added.
        """
        expression = relation.expression
        pairs = []
        for value_set in expression.value_sets():
            ind, val = zip(*((self._variables[variable], float(value))
                             for variable, value in value_set))
            pairs.append(cp.SparsePair(ind=ind, val=val))

        names = [next(self._constr_names) for _ in pairs]

        sense = self.CONSTR_SENSE_MAP[relation.sense]
        self._cp.linear_constraints.add(
            names=names, lin_expr=pairs,
            senses=tuple(repeat(sense, len(pairs))),
            rhs=tuple(repeat(float(-expression.offset), len(pairs))))

        return names

    def add_linear_constraints(self, *relations):
        """Add constraints to the problem

        Each constraint is represented by a Relation, and the
        expression in that relation can be a set expression.
        """
        constraints = []

        for relation in relations:
            if self._check_relation(relation):
                constraints.append(Constraint(self, None))
            else:
                for name in self._add_constraints(relation):
                    constraints.append(Constraint(self, name))

        return constraints

    def set_linear_objective(self, expression):
        """Set linear objective of problem"""

        if isinstance(expression, numbers.Number):
            # Allow expressions with no variables as objective,
            # represented as a number
            expression = Expression()

        # Reset previous objective. We have to build the set of variables to
        # update so that we can avoid calling set_linear if the set is empty.
        # This is due to set_linear failing if the input is an empty
        # iterable.
        reset_vars = set(
            self._variables[var] for var in self._non_zero_objective
            if var not in expression)
        if len(reset_vars) > 0:
            self._cp.objective.set_linear((var, 0) for var in reset_vars)

        # Set actual objective values
        if len(expression.values()) > 0:
            self._cp.objective.set_linear(
                (self._variables[var], value)
                for var, value in expression.values())

        # Keep track of new non-zeros
        self._non_zero_objective = set(expression.variables())

    def set_objective_sense(self, sense):
        """Set type of problem (maximize or minimize)"""
        if sense == ObjectiveSense.Minimize:
            self._cp.objective.set_sense(self._cp.objective.sense.minimize)
        elif sense == ObjectiveSense.Maximize:
            self._cp.objective.set_sense(self._cp.objective.sense.maximize)
        else:
            raise ValueError('Invalid objective sense')

    def _solve(self):
        self._cp.solve()
        if (self._cp.solution.get_status() ==
                self._cp.solution.status.abort_user):
            raise KeyboardInterrupt()

    def solve(self, sense=None):
        """Solve problem"""
        if sense is not None:
            self.set_objective_sense(sense)

        self._solve()
        self._result = Result(self)

        return self._result

    @property
    def result(self):
        return self._result


class Constraint(BaseConstraint):
    """Represents a constraint in a cplex.Problem"""

    def __init__(self, prob, name):
        self._prob = prob
        self._name = name

    def delete(self):
        if self._name is not None:
            self._prob._cp.linear_constraints.delete(self._name)


class Result(BaseResult):
    """Represents the solution to a cplex.Problem

    This object will be returned from the cplex.Problem.solve() method or by
    accessing the cplex.Problem.result property after solving a problem. This
    class should not be instantiated manually.

    Result will evaluate to a boolean according to the success of the
    solution, so checking the truth value of the result will immediately
    indicate whether solving was successful.
    """

    def __init__(self, prob):
        self._problem = prob

    def _check_valid(self):
        if self._problem.result != self:
            raise InvalidResultError()

    @property
    def success(self):
        """Return boolean indicating whether a solution was found"""
        self._check_valid()
        return self._problem._cp.solution.get_status() in (
            self._problem._cp.solution.status.optimal,
            self._problem._cp.solution.status.optimal_tolerance,
            self._problem._cp.solution.status.MIP_optimal)

    @property
    def status(self):
        """Return string indicating the error encountered on failure"""
        self._check_valid()
        return self._problem._cp.solution.get_status_string()

    @property
    def unbounded(self):
        """Whether solution is unbounded"""
        self._check_valid()

        cp = self._problem._cp
        status = cp.solution.get_status()
        presolve = cp.parameters.preprocessing.presolve.get()
        if (status == cp.solution.status.infeasible_or_unbounded and
                presolve):
            # Disable presolve to obtain a definitive answer
            logger.info('Disabling presolver and solving again to determine'
                        ' whether objective is unbounded.')
            cp.parameters.preprocessing.presolve.set(False)
            try:
                self._problem._solve()
            finally:
                cp.parameters.preprocessing.presolve.set(True)

            status = cp.solution.get_status()

        return status == cp.solution.status.unbounded

    def get_value(self, expression):
        """Return value of expression"""

        self._check_valid()
        if isinstance(expression, Expression):
            return sum(self._problem._cp.solution.get_values(
                self._problem._variables[var])*value
                for var, value in expression.values())
        elif expression not in self._problem._variables:
            raise ValueError('Unknown expression: {}'.format(expression))
        return self._problem._cp.solution.get_values(
            self._problem._variables[expression])