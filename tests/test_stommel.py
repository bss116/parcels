from parcels import NEMOGrid, Particle, JITParticle, TimeParticle,\
                    AdvectionRK4, AdvectionEE, AdvectionRK45
from argparse import ArgumentParser
import numpy as np
import math
import pytest
import matplotlib.pyplot as plt
import time


def analytical_eddies_grid(xdim=200, ydim=200):
    """Generate a grid encapsulating the flow field consisting of two
    moving eddies, one moving westward and the other moving northwestward.

    The original test description can be found in: K. Doos,
    J. Kjellsson and B. F. Jonsson. 2013 TRACMASS - A Lagrangian
    Trajectory Model, in Preventive Methods for Coastal Protection,
    T. Soomere and E. Quak (Eds.),
    http://www.springer.com/gb/book/9783319004396
    """
    # Set NEMO grid variables
    depth = np.zeros(1, dtype=np.float32)
    time = np.linspace(0., 100000. * 86400., 2, dtype=np.float64)

    # Coordinates of the test grid (on A-grid in deg)
    lon = np.linspace(0, 60, xdim, dtype=np.float32)
    lat = np.linspace(0, 60, ydim, dtype=np.float32)

    # Define arrays U (zonal), V (meridional), W (vertical) and P (sea
    # surface height) all on A-grid
    U = np.zeros((lon.size, lat.size, time.size), dtype=np.float32)
    V = np.zeros((lon.size, lat.size, time.size), dtype=np.float32)
    P = np.zeros((lon.size, lat.size, time.size), dtype=np.float32)

    # Some constants
    day = 11.6
    r = 1 / (day * 86400)
    beta = 2e-11
    a = 2000000
    e_s = r / (beta * a)

    [x, y] = np.mgrid[:lon.size, :lat.size]
    for t in range(time.size):
        for i in range(lon.size):
            for j in range(lat.size):
                U[i, j, t] = -(1 - math.exp(-lon[i] * math.pi / 180 / e_s) -\
                            lon[i] * math.pi / 180) * math.pi ** 2 *\
                            math.cos(math.pi ** 2 * lat[j] / 180)
                V[i, j, t] = (math.exp(-lon[i] * math.pi / 180 / e_s) / e_s -\
                            1) * math.pi * math.sin(math.pi ** 2 * lat[j] / 180)

    return NEMOGrid.from_data(U, lon, lat, V, lon, lat,
                              depth, time, field_data={'P': P})


def stommel_eddies_example(grid, npart=1, mode='jit', verbose=False,
                          method=AdvectionRK4):
    """Configuration of a particle set that follows two moving eddies

    :arg grid: :class NEMOGrid: that defines the flow field
    :arg npart: Number of particles to intialise"""

    # Determine particle class according to mode
    if mode == 'jit':
        if method == AdvectionRK45:
            raise TypeError('AdvectionRK45 cannot be used in JIT mode')
        ParticleClass = JITParticle
    elif method == AdvectionRK45:
        ParticleClass = TimeParticle
    else:
        ParticleClass = Particle

    pset = grid.ParticleSet(size=npart, pclass=ParticleClass,
                            start=(10., 50.), finish=(7., 30.))
#                            start=(7., 30.), finish=(10., 50.))

    if verbose:
        print("Initial particle positions:\n%s" % pset)

    # Execute for 25 days, with 5min timesteps and hourly output
    hours = 27.635*24.*3600.-330.
    substeps = 1.
    timesteps = 1000.
    dt = hours/timesteps    #To make sure it ends exactly on the end time

    tic = time.clock()

    if method == AdvectionRK45:
        for particle in pset:
            particle.time = 0.
            particle.dt = dt
        tol = 1e-13 #3e-5
        print("Stommel: Advecting %d particles with adaptive step size"
              % (npart))
        pset.execute(method, timesteps=timesteps, dt=dt,
                     output_file=pset.ParticleFile(name="StommelParticle" + method.__name__),
                     output_steps=substeps, tol=tol, flat=True)
    else:
        print("Stommel: Advecting %d particles for %d timesteps"
              % (npart, hours*substeps/dt))
        pset.execute(method, timesteps=timesteps, dt=dt,
                     output_file=pset.ParticleFile(name="StommelParticle" + method.__name__),
                     output_steps=substeps, flat=True)

    toc = time.clock()
    if verbose:
        print("Final particle positions:\n%s" % pset)
        print("Execution time: %f s" % (toc-tic))

    return pset


@pytest.mark.parametrize('mode', ['scipy', 'jit'])
def test_analytic_eddies_grid(mode):
    grid = analytical_eddies_grid()
    pset = stommel_eddies_example(grid, 1, mode=mode)
    assert(pset[0].lon < 0.5 and 45.8 < pset[0].lat < 46.15)
    assert(pset[1].lon < 0.5 and 50.4 < pset[1].lat < 50.7)


if __name__ == "__main__":
    p = ArgumentParser(description="""
Example of particle advection around an idealised peninsula""")
    p.add_argument('mode', choices=('scipy', 'jit'), nargs='?', default='jit',
                   help='Execution mode for performing computation')
    p.add_argument('-p', '--particles', type=int, default=1,
                   help='Number of particles to advect')
    p.add_argument('-v', '--verbose', action='store_true', default=False,
                   help='Print particle information before and after execution')
    p.add_argument('--profiling', action='store_true', default=False,
                   help='Print profiling information after run')
    p.add_argument('-g', '--grid', type=int, nargs=2, default=None,
                   help='Generate grid file with given dimensions')
    p.add_argument('-m', '--method', choices=('RK4', 'EE', 'RK45'), default='RK4',
                   help='Numerical method used for advection')
    args = p.parse_args()
    filename = 'analytical_eddies'

    method = locals()['Advection' + args.method]

    # Generate grid files according to given dimensions
    if args.grid is not None:
        grid = analytical_eddies_grid(args.grid[0], args.grid[1])
        grid.write(filename)

    # Open grid files
    grid = NEMOGrid.from_file(filename)

    if args.profiling:
        from cProfile import runctx
        from pstats import Stats
        runctx("stommel_eddies_example(grid, args.particles, mode=args.mode, \
                              verbose=args.verbose)",
               globals(), locals(), "Profile.prof")
        Stats("Profile.prof").strip_dirs().sort_stats("time").print_stats(10)
    else:
        stommel_eddies_example(grid, args.particles, mode=args.mode,
                              verbose=args.verbose, method=method)