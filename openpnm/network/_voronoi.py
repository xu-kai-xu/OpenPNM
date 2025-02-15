import logging
from openpnm import topotools
from openpnm.network import DelaunayVoronoiDual


logger = logging.getLogger(__name__)
__all__ = ['Voronoi']


class Voronoi(DelaunayVoronoiDual):
    r"""
    Random network formed by Voronoi tessellation of arbitrary base points

    Parameters
    ----------
    points : array_like or int
        Can either be an N-by-3 array of point coordinates which will be
        used, or a scalar value indicating the number of points to
        generate
    shape : array_like
        The size of the domain.  It's possible to create cubic as well as 2D
        square domains by changing the ``shape`` as follows:

            [x, y, z]
                will produce a normal cubic domain of dimension x, and z
            [x, y, 0]
                will produce a 2D square domain of size x by y

    name : str
        An optional name for the object to help identify it. If not given,
        one will be generated.

    Notes
    -----
    By definition these points will each lie in the center of a Voronoi cell,
    so they will not be the pore centers.  The number of pores in the
    returned network thus will differ from the number of points supplied

    """

    def __init__(self, shape=[1, 1, 1], points=None, **kwargs):
        # Clean-up input points
        points = self._parse_points(shape=shape, points=points)
        super().__init__(shape=shape, points=points, **kwargs)
        # Initialize network object
        topotools.trim(network=self, pores=self.pores('delaunay'))
        pop = ['pore.delaunay', 'throat.delaunay', 'throat.interconnect']
        for item in pop:
            del self[item]
