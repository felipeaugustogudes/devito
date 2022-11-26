from devito.finite_differences import IndexDerivative
from devito.ir import Cluster, Interval, IntervalGroup, IterationSpace
from devito.symbolics import retrieve_dimensions, q_leaf, uxreplace
from devito.tools import as_tuple, filter_ordered, timed_pass
from devito.types import Eq, Inc, Spacing, StencilDimension, Symbol

__all__ = ['lower_index_derivatives']


@timed_pass()
def lower_index_derivatives(clusters, sregistry=None, **kwargs):
    processed = []
    weights = {}
    for c in clusters:

        exprs = []
        for e in c.exprs:
            expr, v = _lower_index_derivatives(e, c, weights, sregistry)
            exprs.append(expr)
            processed.extend(v)

        processed.append(c.rebuild(exprs=exprs))

    return processed


def _lower_index_derivatives(expr, c, weights, sregistry):
    """
    Recursively carry out the core of `lower_index_derivatives`.
    """
    if q_leaf(expr):
        return expr, []

    args = []
    processed = []
    for a in expr.args:
        e, clusters = _lower_index_derivatives(a, c, weights, sregistry)
        args.append(e)
        processed.extend(clusters)

    expr = expr.func(*args)

    if not isinstance(expr, IndexDerivative):
        return expr, processed

    dims = retrieve_dimensions(expr, deep=True)
    dims = tuple(filter_ordered(d for d in dims if isinstance(d, StencilDimension)))

    dims = tuple(reversed(dims))

    intervals = [Interval(d, 0, 0) for d in dims]
    ispace0 = IterationSpace(intervals)

    extra = (c.ispace.itdimensions + dims,)
    ispace = IterationSpace.union(c.ispace, ispace0, relations=extra)

    name = sregistry.make_name(prefix='r')
    s = Symbol(name=name, dtype=c.dtype)
    expr0 = Eq(s, 0.)
    ispace1 = ispace.project(lambda d: d is not dims[-1])
    processed.insert(0, c.rebuild(exprs=expr0, ispace=ispace1))

    # Create concrete Weights and reuse them whenever possible
    name = sregistry.make_name(prefix='w')
    w0 = expr.weights.function
    k = tuple(w0.weights)
    try:
        w = weights[k]
    except KeyError:
        w = weights[k] = w0._rebuild(name=name)
    expr = uxreplace(expr, {w0.indexed: w.indexed})

    # Transform e.g. `w[i0] -> w[i0 + 2]` for alignment with the
    # StencilDimensions starting points
    subs = {expr.weights: expr.weights.subs(d, d - d._min) for d in dims}
    expr1 = Inc(s, uxreplace(expr.expr, subs))
    processed.append(c.rebuild(exprs=expr1, ispace=ispace))

    return s, processed
