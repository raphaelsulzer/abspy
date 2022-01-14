"""
Benchmarking adaptive binary space partitioning with exhaustive hyperplane arrangement.

"""

import numpy as np
import glob
import time
from pathlib import Path

from abspy import attach_to_log
from abspy import VertexGroup
from abspy import CellComplex

logger = attach_to_log(filepath='benchmark.log')

sage_installed = True
save_file = False

try:
    from sage.all import *
    logger.info('SageMath installation found')
except ModuleNotFoundError:
    logger.warning('SageMath is not installed. hyperplane arrangement benchmark will not run')
    sage_installed = False


def pipeline_hyperplane_arrangement(planes):
    """
    Hyperplane arrangement with SageMath.
    The SageMath binaries can be downloaded from https://www.sagemath.org/download.html.
    The installation of it is documented at https://doc.sagemath.org/html/en/installation/.
    """
    # hyperplane arrangements and bounded region extraction
    logger.info('starting hyperplane arrangement')
    hyperplane_arrangement = HyperplaneArrangements(QQ, ('x', 'y', 'z'))
    arrangements = hyperplane_arrangement([[tuple(plane[:3]), plane[-1]] for plane in planes])
    convexes = arrangements.bounded_regions()
    logger.info('number of cells: {}'.format(len(convexes)))


def pipeline_adaptive_binary_partition(planes, bounds, filename=None):
    """
    Adaptive binary partition as implemented.
    """
    tik = time.time()
    cell_complex = CellComplex(planes, bounds, build_graph=True)
    cell_complex.prioritise_planes()
    cell_complex.construct()
    cell_complex.print_info()
    logger.info('runtime pipeline_adaptive_binary_partition: {:.2f} s\n'.format(time.time() - tik))

    if save_file and filename and filename.suffix == '.obj':
        cell_complex.save_obj(filepath=Path(filename).with_suffix('.obj'))
    if save_file and filename and filename.suffix == '.plm':
        cell_complex.save_plm(filepath=Path(filename).with_suffix('.plm'))


def pipeline_exhaustive_binary_partition(planes, bounds, filename=None):
    """
    Exhaustive binary partition as implemented.
    """
    tik = time.time()
    cell_complex = CellComplex(planes, bounds, build_graph=True)
    cell_complex.prioritise_planes()
    cell_complex.construct(exhaustive=True)
    cell_complex.print_info()
    logger.info('runtime pipeline_exhaustive_binary_partition: {:.2f} s\n'.format(time.time() - tik))

    if save_file and filename and filename.suffix == '.obj':
        cell_complex.save_obj(filepath=Path(filename).with_suffix('.obj'))
    if save_file and filename and filename.suffix == '.plm':
        cell_complex.save_plm(filepath=Path(filename).with_suffix('.plm'))


def run_benchmark(data_dir='./data/*.vg'):
    """
    :param data_dir: dir of .vg file(s) generated by Mapple.
    """

    logger.info('---------- start benchmarking ----------')

    for filename in glob.glob(data_dir)[:]:

        # Fig4f and Fig4i are defected: having vertex groups of 2 points. failing at PCA calculation
        if 'Fig4f' in filename or 'Fig4i' in filename or not filename.endswith('.vg'):
            continue

        vertex_group = VertexGroup(filepath=filename)
        planes, bounds = np.array(vertex_group.planes), np.array(vertex_group.bounds)

        pipeline_adaptive_binary_partition(planes, bounds, filename=Path(filename).with_suffix('.plm'))
        pipeline_exhaustive_binary_partition(planes, bounds, filename=Path(filename).with_suffix('.plm'))

        if sage_installed:
            tok = time.time()
            pipeline_hyperplane_arrangement(planes)
            logger.info('runtime pipeline_hyperplane_arrangement: {:.2f} s\n'.format(time.time() - tok))


def plot_benchmark(filepath=None, records=('absp', 'sage'), **kwargs):
    """
    Plotting computation complexity with number of planar primitives.
    :param filepath: path to the saved log.
    :param records: 'absp' or 'sage' or both. both are needed for loading, but can plot one only.
    :param kwargs: dict of num_planes, num_cells_sage, num_cells_ours, runtime_sage, runtime_ours
    """
    from matplotlib.pylab import legend, show, scatter, figure

    if not (filepath or kwargs):
        raise ValueError('no filepath or data array as input!')

    if not set(records).issubset({'absp', 'sage'}):
        raise KeyError("records has to be 'absp' or 'sage', or both!")

    if kwargs:
        num_planes = kwargs['num_planes']
        num_cells_sage = kwargs['num_cells_sage']
        num_cells_absp = kwargs['num_cells_absp']
        runtime_sage = kwargs['runtime_sage']
        runtime_absp = kwargs['runtime_absp']

    else:
        num_planes = []
        num_cells_sage = []
        num_cells_absp = []
        runtime_absp = []
        runtime_sage = []

        with open(filepath, 'r') as f:
            count = 0  # to distinguish number of cells for the two pipelines
            for line in f.readlines():
                if 'number of planes' in line:
                    num_planes.append(int(line.split()[-1]))
                elif 'runtime pipeline_adaptive_binary_partition' in line:
                    runtime_absp.append(float(line.split()[-2]))
                elif 'runtime pipeline_hyperplane_arrangement' in line:
                    runtime_sage.append(float(line.split()[-2]))
                elif 'number of cells' in line:
                    if count % 2:
                        num_cells_sage.append(int(line.split()[-1]))
                    else:
                        num_cells_absp.append(int(line.split()[-1]))
                    count += 1

    x, ya, yb, yc, yd = num_planes, runtime_sage, runtime_absp, num_cells_sage, num_cells_absp

    fig = figure()

    # runtime
    ax_ab = fig.add_subplot(121)
    ax_ab.set_xlabel('# primitives')
    ax_ab.set_ylabel('runtime (s)')
    if 'sage' in records:
        scatter(x, ya, label='sage')
    if 'absp' in records:
        scatter(x, yb, label='absp')
    legend()

    # cell number
    ax_cd = fig.add_subplot(122)
    ax_cd.set_xlabel('# primitives')
    ax_cd.set_ylabel('# cells')
    if 'sage' in records:
        scatter(x, yc, label='sage')
    if 'absp' in records:
        scatter(x, yd, label='absp')
    legend()

    show()


if __name__ == '__main__':
    run_benchmark()
    # plot_benchmark(filepath='./benchmark.log', records={'absp', 'sage'})
