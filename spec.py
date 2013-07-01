import simsym
import z3
import z3printer
import collections
import itertools
import sys
import argparse
import os

def test(base, *calls):
    all_s = []
    all_r = []

    for callseq in itertools.permutations(range(0, len(calls))):
        s = base.var(base.__name__)
        r = {}
        seqname = ''.join(map(lambda i: chr(i + ord('a')), callseq))
        for idx in callseq:
            r[idx] = calls[idx](s, chr(idx + ord('a')), seqname)
        all_s.append(s)
        all_r.append(r)

    diverge = ()
    if simsym.symor([all_r[0] != r for r in all_r[1:]]):
        diverge += ('results',)
    if simsym.symor([all_s[0] != s for s in all_s[1:]]):
        diverge += ('state',)

    ## XXX precisely keeping track of what diverges incurs overhead.
    ## Avoid the needless book-keeping for now.
    if len(diverge) == 0: return ()
    return ('something',)

    return diverge

def contains_var(expr):
    if z3.is_var(expr):
        return True
    return any([contains_var(child) for child in expr.children()])

def fnmap(x, fnlist):
    for f in fnlist:
        match = False
        fl = f.as_list()
        for fk, fv in fl[:-1]:
            if fk.eq(x):
                x = fv
                match = True
        if not match:
            x = fl[-1]
    return x

def var_unwrap(e, fnlist, modelctx):
    if not contains_var(e):
        return None
    if z3.is_var(e) and z3.get_var_index(e) == 0:
        fn0 = fnlist[0].as_list()
        retlist = []
        for fkey, fval in fn0[:-1]:
            retlist.append([fkey, fnmap(fval, fnlist[1:])])
        retlist.append(fnmap(fn0[-1], fnlist[1:]))
        return retlist
    if e.num_args() != 1:
        raise Exception('cannot var_unwrap: %s' % str(e))
    arg = e.arg(0)
    f = e.decl()
    return var_unwrap(arg, [modelctx[f]] + fnlist, modelctx)

class IsomorphicMatch(object):
    ## Originally based on http://stackoverflow.com/questions/11867611

    ## We need to construct a condition for two assignments being isomorphic
    ## to each other.  This is interesting for uninterpreted sorts, where
    ## we don't care about the specific value assignment from Z3, and care
    ## only about whether the equality pattern looks the same.  This is
    ## made more complicated by the fact that uninterpreted sorts show up
    ## all over the place: as values of a variable, as values in an array,
    ## as keys in an array, as default 'else' values in an array, etc.

    ## XXX handling FDs and timestamps might be better done by treating
    ## them as supporting order, rather than supporting just equality;
    ## the isomorphism condition would be the values being in the same
    ## order, rather than in the same equality pattern.

    def __init__(self, model):
        self.uninterps = collections.defaultdict(list)
        self.conds = [z3.BoolVal(True)]

        # Try to reach a fixed-point with expressions of uninterpreted
        # sorts used in array indexes.
        self.groups_changed = True
        while self.groups_changed:
            self.groups_changed = False
            self.process_model(model)

        self.process_uninterp()

    def process_model(self, model):
        for decl in model:
            ## Do not bother including "internal" variables in the wrapped model;
            ## otherwise Z3 can iterate over different assignments to these
            ## variables, while we care only about assignments to "external"
            ## variables.
            if '!' in str(decl) or 'internal_' in str(decl) or 'dummy_' in str(decl):
                continue
            self.process_decl_assignment(decl, model[decl], model)

    def process_decl_assignment(self, decl, val, model):
        """Process a single assignment in a model.

        decl must be a z3.FuncDeclRef assigned to z3.ExprRef val in
        the given model.
        """

        if decl.arity() > 0:
            raise Exception('handle nonzero arity')

            ## Handle FuncDeclRef objects -- XXX old code.
            assert(decl.arity() == 1)

            val_list = val.as_list()
            for valarg, valval in val_list[:-1]:
                self.add_equal(decl(self.uwrap(valarg)), valval)

            domain_sorts = [decl.domain(i) for i in range(0, decl.arity())]
            domain_anon = [z3.Const(simsym.anon_name(), s) for s in domain_sorts]
            elsecond = z3.ForAll(domain_anon,
                          z3.Or([decl(*domain_anon) == self.uwrap(val_list[-1])] +
                                [domain_anon[0] == self.uwrap(x)
                                 for x, _ in val_list[:-1]]))
            self.conds.append(elsecond)
            return

        # Calling a FuncDeclRef returns the Z3 function application
        # expression (which, since we weeded out non-zero arity above,
        # will be just the constant being bound by the model,
        # satisfying is_app and is_const).
        dconst = decl()

        symtype = simsym.symbolic_type(dconst)
        self.process_const_assignment(dconst, val, symtype, model)

    def process_const_assignment(self, dconst, val, symtype, model):
        """Process a single constant assignment in model.

        dconst is a projected constant expression, which is either a
        Z3 constant expression, a projection of a projected constant
        expression, or a select of a projected constant expression.
        val is the assignment of dconst in model.  The sort of dconst
        must agree with the type of val (for primitive sorts, they are
        equal; for array sorts, val must be a FuncInterp).  symtype is
        the pseudo-Symbolic type of dconst, as defined by
        simsym.symbolic_type.

        Effectively, this starts with the assignment (dconst == val)
        and recursively decomposes compound values on both sides until
        both dconst and val are primitive sorts.  At this point it
        calls add_assignment to register the primitive assignment.
        """

        dsort = dconst.sort()

        if dsort.kind() == z3.Z3_DATATYPE_SORT:
            raise Exception("Z3_DATATYPE_SORT in process_const_assignment")
            # XXX Should be unused now.  If we do still need this, we
            # need to flow symtype through below.
            nc = None
            for i in range(0, dsort.num_constructors()):
                if val.decl().eq(dsort.constructor(i)): nc = i
            if nc is None:
                raise Exception('Could not find constructor for %s' % str(dconst))
            for i in range(0, dsort.constructor(nc).arity()):
                dconst_field = dsort.accessor(nc, i)(dconst)
                childval = val.children()[i]
                self.process_const_assignment(dconst_field, childval, model)
            return

        if dsort.kind() in [z3.Z3_INT_SORT,
                            z3.Z3_BOOL_SORT,
                            z3.Z3_UNINTERPRETED_SORT]:
            if not z3.is_const(val):
                print 'WARNING: Not a constant:', val
            assert issubclass(symtype, simsym.Symbolic)
            self.add_assignment(dconst, val, symtype)
            return

        if dsort.kind() == z3.Z3_ARRAY_SORT:
            if z3.is_as_array(val):
                func_interp = model[z3.get_as_array_func(val)]
            else:
                func_interp = val
            assert(isinstance(func_interp, z3.FuncInterp))

            flist = func_interp.as_list()

            assert isinstance(symtype, tuple)

            ## Sometimes Z3 gives us assignments like:
            ##   k!21594 = [else -> k!21594!21599(k!21597(Var(0)))],
            ##   k!21597 = [Fn!val!1 -> Fn!val!1, else -> Fn!val!0],
            ##   k!21594!21599 = [Fn!val!0 -> True, else -> False],
            ## Check if flist[0] contains a Var() thing; if so, unwrap the Var.
            if len(flist) == 1:
                var_flist = var_unwrap(flist[0], [], model)
                if var_flist is not None:
                    flist = var_flist

            ## Handle everything except the "else" value
            for fidx, fval in flist[:-1]:
                fidxrep = self.uninterp_representative(fidx)
                if fidxrep is None: continue
                self.process_const_assignment(
                    dconst[fidxrep], fval, symtype[1], model)

            ## One problem is what to do with ArrayRef assignments (in the form of
            ## a FuncInterp), because FuncInterp assigns a value for every index,
            ## but we only care about specific indexes.  (It's not useful to receive
            ## another model that differs only in some index we never cared about.)
            ## To deal with this problem, we add FuncInterp constraints only for
            ## indexes that are interesting.  For uninterpreted sorts, this
            ## is the universe of values for that sort.  For interpreted sorts
            ## (integers), we add constraints for values explicitly listed in
            ## the FuncInterp, and skip the "else" clause altogether.  This is
            ## imprecise: it means self.conds is less constrained than it should
            ## be, so its negation is too strict, and might preclude some
            ## otherwise-interesting assignments.

            if dconst.domain().kind() == z3.Z3_UNINTERPRETED_SORT:
                univ = model.get_universe(dconst.domain())
                if univ is None: univ = []
                for idx in univ:
                    if any([idx.eq(i) for i, _ in flist[:-1]]): continue
                    idxrep = self.uninterp_representative(idx)
                    if idxrep is None: continue
                    self.process_const_assignment(
                        dconst[idxrep], flist[-1], symtype[1], model)
            return

        print dsort.kind()
        raise Exception('handle %s = %s' % (dconst, val))

    def uninterp_groups(self, sort):
        groups = []
        for expr, val in self.uninterps[sort]:
            found = False
            for group_val, group_exprs in groups:
                if val.eq(group_val):
                    group_exprs.append(expr)
                    found = True
            if not found:
                groups.append((val, [expr]))
        return groups

    def uninterp_representative(self, val):
        for expr2, val2 in self.uninterps[val.sort()]:
            if val.eq(val2):
                return expr2
        return None

    def add_assignment(self, expr, val, symtype):
        pseudo_sort = isomorphism_types.get(symtype)
        if pseudo_sort == "ignore":
            return

        sort = val.sort()
        if sort.kind() == z3.Z3_UNINTERPRETED_SORT or pseudo_sort == "equal":
            # XXX This is buggy: for interpreted sorts, since we group
            # by Z3 sort, we may group things that have the same Z3
            # sort but different simsym synonym types.  Luckily, we
            # currently only have one such pseudo-sort, so we're
            # actually okay.  We could pass symtype instead of sort
            # were it not for uninterp_representative.
            self.add_assignment_uninterp(expr, val, sort)
            return

        if expr.sort().kind() != z3.Z3_BOOL_SORT:
            print 'WARNING: Interpreted sort assignment:', expr, val

        cond = (expr == val)
        if not any([c.eq(cond) for c in self.conds]):
            self.conds.append(cond)

    def add_assignment_uninterp(self, expr, val, sort):
        new_group = True
        for uexpr, uval in self.uninterps[sort]:
            if uval.eq(val):
                new_group = False
                if uexpr.eq(expr): return
        if new_group:
            self.groups_changed = True
        self.uninterps[sort].append((expr, val))

    def process_uninterp(self):
        for sort in self.uninterps:
            groups = self.uninterp_groups(sort)
            for _, exprs in groups:
                for otherexpr in exprs[1:]:
                    self.conds.append(exprs[0] == otherexpr)
            representatives = [exprs[0] for _, exprs in groups]
            if len(representatives) > 1:
                self.conds.append(z3.Distinct(representatives))

    def notsame_cond(self):
        return simsym.wrap(z3.Not(z3.And(self.conds)))

class TestWriter(object):
    def __init__(self, model_file=None, test_file=None):
        if isinstance(model_file, basestring):
            model_file = open(model_file, 'w')
        self.model_file, self.test_file = model_file, test_file
        if test_file and testgen:
            self.testgen = testgen(test_file)
        else:
            self.testgen = None

    def begin_call_set(self, callset):
        if self.model_file:
            print >> self.model_file, "=== Models for %s ===" % \
                " ".join(c.__name__ for c in callset)
            print >> self.model_file

        self.callset = callset
        self.npath = self.ncompath = self.nmodel = 0

        if self.testgen:
            self.testgen.begin_call_set(callset)

    def on_result(self, result):
        self.npath += 1

        # Filter out non-commutative results
        if result.value != ():
            self.__progress(False)
            return

        self.ncompath += 1

        if not self.model_file and not self.testgen:
            self.__progress(False)
            return

        if self.model_file:
            print >> self.model_file, "== Path %d ==" % self.ncompath
            print >> self.model_file

        e = result.path_condition

        ## This can potentially reduce the number of test cases
        ## by, e.g., eliminating irrelevant variables from e.
        ## The effect doesn't seem significant: one version of Fs
        ## produces 3204 test cases without simplify, and 3182 with.
        e = simsym.simplify(e)

        while self.nmodel < args.max_testcases:
            # XXX Would it be faster to reuse the solver?
            check, model = simsym.check(e)
            if check == z3.unsat: break
            if check == z3.unknown:
                # raise Exception('Cannot enumerate: %s' % str(e))
                print 'Cannot enumerate, moving on..'
                print 'Failure reason:', model
                break

            # Construct the isomorphism condition for model.  We do
            # this before __on_model, since that may perform model
            # completion, which may add more variable assignments to
            # the model.
            same = IsomorphicMatch(model)

            self.__on_model(result, model)

            notsame = same.notsame_cond()
            if args.verbose_testgen:
                print 'Negation', self.nmodel, ':', notsame
            e = simsym.symand([e, notsame])

            self.__progress(False)
        self.__progress(False)

    def __on_model(self, result, model):
        self.nmodel += 1

        if self.model_file:
            print >> self.model_file, model.sexpr()
            print >> self.model_file
            self.model_file.flush()

        if self.testgen:
            self.testgen.on_model(result, result.get_model(model))

    def __progress(self, end):
        if os.isatty(sys.stdout.fileno()):
            sys.stdout.write('\r')
        elif not end:
            return
        sys.stdout.write('  %d paths (%d commutative), %d testcases' % \
                         (self.npath, self.ncompath, self.nmodel))
        if os.isatty(sys.stdout.fileno()):
            # Clear to end of line
            sys.stdout.write('\033[K')
            if end:
                sys.stdout.write('\n')
            else:
                # Put cursor in wrap-around column.  If we print
                # anything more after this, it will immediately wrap
                # and print on the next line.  But we can still \r to
                # overwrite this line with another progress update.
                sys.stdout.write('\033[K\033[999C ')
        else:
            sys.stdout.write('\n')
        sys.stdout.flush()

    def end_call_set(self):
        if self.testgen:
            self.testgen.end_call_set()
        self.__progress(True)

    def finish(self):
        if self.testgen:
            self.testgen.finish()

parser = argparse.ArgumentParser()
parser.add_argument('-c', '--check-conds', action='store_true',
                    help='Check commutativity conditions for sat/unsat')
parser.add_argument('-p', '--print-conds', action='store_true',
                    help='Print commutativity conditions')
parser.add_argument('-m', '--model-file',
                    help='Z3 model output file')
parser.add_argument('-t', '--test-file',
                    help='Test generator output file')
parser.add_argument('-n', '--ncomb', type=int, default=2, action='store',
                    help='Number of system calls to combine per test')
parser.add_argument('-f', '--functions', action='store',
                    help='Methods to run (e.g., stat,fstat)')
parser.add_argument('--simplify-more', default=False, action='store_true',
                    help='Use ctx-solver-simplify')
parser.add_argument('--max-testcases', type=int, default=sys.maxint, action='store',
                    help='Maximum # test cases to generate per combination')
parser.add_argument('--verbose-testgen', default=False, action='store_true',
                    help='Print diagnostics during model enumeration')
parser.add_argument('module', metavar='MODULE', default='fs', action='store',
                    help='Module to test (e.g., fs)')
args = parser.parse_args()

def print_cond(msg, cond):
    if args.check_conds and simsym.check(cond)[0] == z3.unsat:
        return

    ## If the assumptions (i.e., calls to simsym.assume) imply the condition
    ## is true, we say that condition always holds, and we can print "always".
    ## It would be nice to print a clean condition that excludes assumptions,
    ## even if the assumptions don't directly imply the condition, but that
    ## would require finding the simplest expression for x such that
    ##
    ##   x AND simsym.assume_list = cond
    ##
    ## which seems hard to do using Z3.  In principle, this should be the
    ## same as simplifying the 'c' expression below, but Z3 isn't good at
    ## simplifying it.  We could keep the two kinds of constraints (i.e.,
    ## explicit assumptions vs. symbolic execution control flow constraints)
    ## separate in simsym, which will make them easier to disentangle..

    #c = simsym.implies(simsym.symand(simsym.assume_list), cond)
    ## XXX the above doesn't work well -- it causes open*open to say "always".
    ## One hypothesis is that we should be pairing the assume_list with each
    ## path condition, instead of taking the assume_list across all paths.
    c = cond

    if args.check_conds and simsym.check(simsym.symnot(c))[0] == z3.unsat:
        s = 'always'
    else:
        if args.print_conds:
            scond = simsym.simplify(cond, args.simplify_more)
            s = '\n    ' + str(scond).replace('\n', '\n    ')
        else:
            if args.check_conds:
                s = 'sometimes'
            else:
                s = 'maybe'
    print '  %s: %s' % (msg, s)

z3printer._PP.max_lines = float('inf')
m = __import__(args.module)
base = m.model_class
testgen = m.model_testgen if hasattr(m, 'model_testgen') else None
if testgen is None and args.test_file:
    parser.error("No test case generator for this module")

test_writer = TestWriter(args.model_file, args.test_file)

isomorphism_types = getattr(m, 'isomorphism_types', {})

if args.functions is not None:
    calls = [getattr(base, fname) for fname in args.functions.split(',')]
else:
    calls = m.model_functions

for callset in itertools.combinations_with_replacement(calls, args.ncomb):
    print ' '.join([c.__name__ for c in callset])
    test_writer.begin_call_set(callset)

    condlists = collections.defaultdict(list)
    for sar in simsym.symbolic_apply(test, base, *callset):
        condlists[sar.value].append(sar.path_condition)
        test_writer.on_result(sar)

    test_writer.end_call_set()

    conds = collections.defaultdict(lambda: [simsym.wrap(z3.BoolVal(False))])
    for result, condlist in condlists.items():
        conds[result] = condlist

    # Internal variables help deal with situations where, for the same
    # assignment of initial state + external inputs, two operations both
    # can commute and can diverge (depending on internal choice, like the
    # inode number for file creation).
    commute = simsym.symor(conds[()])
    cannot_commute = simsym.symnot(simsym.exists(simsym.internals(), commute))

    for diverge, condlist in sorted(conds.items()):
        if diverge == ():
            print_cond('can commute', simsym.symor(condlist))
        else:
            print_cond('cannot commute, %s can diverge' % ', '.join(diverge),
                       simsym.symand([simsym.symor(condlist), cannot_commute]))

test_writer.finish()
