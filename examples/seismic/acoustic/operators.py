from sympy import solve, Symbol

from devito import Eq, Operator, Function, TimeFunction, SubDimension
from devito.logger import error
from examples.seismic import PointSource, Receiver, ABC


def inner_ind(model):
    """
    Dimensions of the inner part of the domain without ABC layers
    """
    sub_dim =[]
    for dim in model.grid.dimensions:
        sub_dim += [SubDimension(name=dim.name + '_in', parent=dim,
                                 lower=model.nbpml, upper=-model.nbpml)]

    return tuple(sub_dim)

def laplacian(field, m, s, kernel):
    """
    Spacial discretization for the isotropic acoustic wave equation. For a 4th
    order in time formulation, the 4th order time derivative is replaced by a
    double laplacian:
    H = (laplacian + s**2/12 laplacian(1/m*laplacian))
    :param field: Symbolic TimeFunction object, solution to be computed
    :param m: square slowness
    :param s: symbol for the time-step
    :return: H
    """
    biharmonic = field.laplace2(1/m) if kernel == 'OT4' else 0
    return field.laplace + s**2/12 * biharmonic

def iso_stencil(field, kernel, model, s, **kwargs):
    """
    Stencil for the acoustic isotropic wave-equation:
    u.dt2 - H + damp*u.dt = 0
    :param field: Symbolic TimeFunction object, solution to be computed
    :param m: square slowness
    :param s: symbol for the time-step
    :param damp: ABC dampening field (Function)
    :param kwargs: forwad/backward wave equation (sign of u.dt will change accordingly
    as well as the updated time-step (u.forwad or u.backward)
    :return: Stencil for the wave-equation
    """

    # Creat a temporary symbol for H to avoid expensive sympy solve
    H = Symbol('H')
    # Define time sep to be updated
    next = field.forward if kwargs.get('forward', True) else field.backward
    # Define PDE
    eq = model.m * field.dt2 - H - kwargs.get('q', 0)
    # Solve the symbolic equation for the field to be updated
    eq_time = solve(eq, next, rational=False, simplify=False)[0]
    # Get the spacial FD
    lap = laplacian(field, model.m, s, kernel)
    # return the Stencil with H replaced by its symbolic expression
    inner = [(i, i_in) for i, i_in in zip(model.grid.dimensions, inner_ind(model))]
    return [Eq(next, eq_time.subs({H: lap})).subs(inner)]


def ForwardOperator(model, source, receiver, space_order=4,
                    save=False, kernel='OT2', **kwargs):
    """
    Constructor method for the forward modelling operator in an acoustic media

    :param model: :class:`Model` object containing the physical parameters
    :param source: :class:`PointData` object containing the source geometry
    :param receiver: :class:`PointData` object containing the acquisition geometry
    :param space_order: Space discretization order
    :param save: Saving flag, True saves all time steps, False only the three
    """

    # Create symbols for forward wavefield, source and receivers
    u = TimeFunction(name='u', grid=model.grid,
                     save=source.nt if save else None,
                     time_order=2, space_order=space_order)
    src = PointSource(name='src', grid=model.grid, time_range=source.time_range,
                      npoint=source.npoint)
    rec = Receiver(name='rec', grid=model.grid, time_range=receiver.time_range,
                   npoint=receiver.npoint)

    s = model.grid.stepping_dim.spacing
    eqn = iso_stencil(u, kernel, model, s)

    # Construct expression to inject source values
    src_term = src.inject(field=u.forward, expr=src * s**2 / model.m,
                          offset=model.nbpml)

    # Create interpolation expression for receivers
    rec_term = rec.interpolate(expr=u, offset=model.nbpml)

    BC = ABC(model, u, model.m)
    eq_abc = BC.abc

    # Substitute spacing terms to reduce flops
    return Operator(eqn + eq_abc + src_term + rec_term, subs=model.spacing_map,
                    name='Forward', **kwargs)


def AdjointOperator(model, source, receiver, space_order=4,
                    kernel='OT2', **kwargs):
    """
    Constructor method for the adjoint modelling operator in an acoustic media

    :param model: :class:`Model` object containing the physical parameters
    :param source: :class:`PointData` object containing the source geometry
    :param receiver: :class:`PointData` object containing the acquisition geometry
    :param time_order: Time discretization order
    :param space_order: Space discretization order
    """

    v = TimeFunction(name='v', grid=model.grid, save=None,
                     time_order=2, space_order=space_order)
    srca = PointSource(name='srca', grid=model.grid, time_range=source.time_range,
                       npoint=source.npoint)
    rec = Receiver(name='rec', grid=model.grid, time_range=receiver.time_range,
                   npoint=receiver.npoint)

    s = model.grid.stepping_dim.spacing
    eqn = iso_stencil(v, kernel, model, s, forward=False)

    # Construct expression to inject receiver values
    receivers = rec.inject(field=v.backward, expr=rec * s**2 / model.m,
                           offset=model.nbpml)

    # Create interpolation expression for the adjoint-source
    source_a = srca.interpolate(expr=v, offset=model.nbpml)

    BC = ABC(model, v, model.m, forward=False)
    eq_abc = BC.abc

    # Substitute spacing terms to reduce flops
    return Operator(eqn + eq_abc + receivers + source_a, subs=model.spacing_map,
                    name='Adjoint', **kwargs)


def GradientOperator(model, source, receiver, space_order=4, save=True,
                     kernel='OT2', **kwargs):
    """
    Constructor method for the gradient operator in an acoustic media

    :param model: :class:`Model` object containing the physical parameters
    :param source: :class:`PointData` object containing the source geometry
    :param receiver: :class:`PointData` object containing the acquisition geometry
    :param time_order: Time discretization order
    :param space_order: Space discretization order
    """

    # Gradient symbol and wavefield symbols
    grad = Function(name='grad', grid=model.grid)
    u = TimeFunction(name='u', grid=model.grid, save=source.nt if save
                     else None, time_order=2, space_order=space_order)
    v = TimeFunction(name='v', grid=model.grid, save=None,
                     time_order=2, space_order=space_order)
    rec = Receiver(name='rec', grid=model.grid,
                   time_range=receiver.time_range, npoint=receiver.npoint)

    s = model.grid.stepping_dim.spacing
    eqn = iso_stencil(v, kernel, model, s, forward=False)

    if kernel == 'OT2':
        gradient_update = Eq(grad, grad - u.dt2 * v)
    elif kernel == 'OT4':
        gradient_update = Eq(grad, grad - (u.dt2 +
                                           s**2 / 12.0 * u.laplace2(model.m**(-2))) * v)

    # Add expression for receiver injection
    receivers = rec.inject(field=v.backward, expr=rec * s**2 / model.m,
                           offset=model.nbpml)
    BC = ABC(model, v, model.m, forward=False)
    eq_abc = BC.abc
    # Substitute spacing terms to reduce flops
    return Operator(eqn + receivers + eq_abc + [gradient_update], subs=model.spacing_map,
                    name='Gradient', **kwargs)

def BornOperator(model, source, receiver, space_order=4,
                 kernel='OT2', **kwargs):
    """
    Constructor method for the Linearized Born operator in an acoustic media

    :param model: :class:`Model` object containing the physical parameters
    :param source: :class:`PointData` object containing the source geometry
    :param receiver: :class:`PointData` object containing the acquisition geometry
    :param time_order: Time discretization order
    :param space_order: Space discretization order
    """

    # Create source and receiver symbols
    src = PointSource(name='src', grid=model.grid, time_range=source.time_range,
                      npoint=source.npoint)
    rec = Receiver(name='rec', grid=model.grid, time_range=receiver.time_range,
                   npoint=receiver.npoint)

    # Create wavefields and a dm field
    u = TimeFunction(name="u", grid=model.grid, save=None,
                     time_order=2, space_order=space_order)
    U = TimeFunction(name="U", grid=model.grid, save=None,
                     time_order=2, space_order=space_order)
    dm = Function(name="dm", grid=model.grid, space_order=0)

    s = model.grid.stepping_dim.spacing
    eqn1 = iso_stencil(u, kernel, model, s)
    eqn2 = iso_stencil(U, kernel, model, s, q=-dm*u.dt2)

    # Add source term expression for u
    source = src.inject(field=u.forward, expr=src * s**2 / model.m,
                        offset=model.nbpml)

    # Create receiver interpolation expression from U
    receivers = rec.interpolate(expr=U, offset=model.nbpml)

    eq_abc = ABC(model, u, model.m).abc
    eq_abcl = ABC(model, U, model.m).abc

    # Substitute spacing terms to reduce flops
    return Operator(eqn1 + eq_abc + source + eqn2 + eq_abcl + receivers, subs=model.spacing_map,
                    name='Born', **kwargs)
