import logging
import math
import numpy as np
from scipy import ndimage
import scipy.spatial as sptl
from openpnm import topotools
from scipy.spatial import ConvexHull
import openpnm.models.geometry as gm
from openpnm.utils import Project
from openpnm.network import DelaunayVoronoiDual
from transforms3d import _gohlketransforms as tr
from openpnm.geometry import GenericGeometry
from openpnm.utils import unique_list

logger = logging.getLogger(__name__)


class VoronoiFibers(Project):
    r"""
    Resembles a fibrous paper or mat with straight intersecting fibers.

    Two geometries are created: DelaunayGeometry defines the pore space
    with pores connected by a Delaunay tesselation and VoronoiGeometry defines
    the fiber space with fibers forming the edges of the Voronoi diagram.
    The two geometries are complimentary and can be accessed individually
    via the project associated with this network object.

    This class is a subclass of the DelaunayVoronoiDual network, and many of
    the parameters behave in the exact same way. However, currently a
    rectangular shape is the only one supported. Additionally, an image is
    created for calculating size data and two parameters relate to this:
    fiber_rad and resolution as detailed below.

    Parameters
    ----------
    points : array_like or int
        Can either be an N-by-3 array of point coordinates which will be used,
        or a scalar value indicating the number of points to generate

    shape : array_like
        The size and shape of the domain using for generating and trimming
        excess points. It's treated as the outer corner of rectangle [x, y, z]
        whose opposite corner lies at [0, 0, 0].

        By default, a domain size of [1, 1, 1] is used.

    fiber_rad: float
        fiber radius to apply to Voronoi edges when calculating pore and throat
        sizes

    resolution : bool
        Determines the size of each voxel in the image. Care should be made to
        appropriately set the resolution based on the fiber_radius and the
        shape of the domain so as to remain within memory constraints.

    linear_scale : array_like (len 3)
        This is applied to the domain size before placing the points and then
        reversed afterwards and has the effect of introducing anisotropy into
        an otherwise uniformly distributed point distribution. By default no
        scaling is applied. Applying [1, 1, 2] will stretch the domain by a
        factor of 2 in the z-direction and this will have the affect of
        aligning fibers in the x and y directions once scaling is reversed.

    References
    ----------
    This approach to modeling fibrous materials was first presented by
    Thompson [1] for simulating fluid imbibition in sorbent paper products.
    Gostick [2], and Tranter et al.[3, 4] have subsequently used it to model
    electrodes in fuel cells.

    [1] K. E. Thompson, AlChE J., 48, 1369 (2002)
    [2] J. T. Gostick, Journal of the Electrochemical Society 2013, 160, F731.
    [3] T. G. Tranter et al. Fuel Cells, 2016, 16, 4, 504-515
    [4] T. G. Tranter et al. Transport in Porous Media, 2018, 121, 3, 597-620

    Examples
    --------
    Points will be automatically generated if none are given:

    >>> import openpnm as op
    >>> ws = op.Workspace()
    >>> ws.clear()
    >>> prj = op.materials.VoronoiFibers(points=10,
    ...                                  shape=[1e-4, 1e-4, 1e-4],
    ...                                  fiber_rad=5e-6,
    ...                                  resolution=2e-6)
    """

    def __init__(self,
                 points=None,
                 fiber_rad=10,
                 resolution=1e-2,
                 shape=[1, 1, 1],
                 linear_scale=None,
                 name=None,
                 **kwargs):

        super().__init__(name=name)
        shape = np.array(shape)
        scale_applied = False
        if linear_scale is not None:
            if len(linear_scale) != 3:
                logger.exception(
                    msg="linear_scale must have length 3 " + "to scale each axis"
                )
            else:
                ls = np.asarray(linear_scale)
                shape *= ls
                scale_applied = True
        #                net['pore.coords'] *= ls
        if (len(shape) != 3) or np.any(shape == 0):
            raise Exception("Only 3D, rectangular shapes are supported")
        if fiber_rad is None:
            logger.exception(msg="Please initialize class with a fiber_rad")

        net = DelaunayVoronoiDual(project=self,
                                  points=points,
                                  shape=shape,
                                  name=self.name + "_net",
                                  **kwargs)
        if scale_applied:
            net["pore.coords"] /= ls
        net.fiber_rad = fiber_rad
        net.resolution = resolution
        del_geom = DelaunayGeometry(project=self,
                                    network=net,
                                    pores=net.pores("delaunay"),
                                    throats=net.throats("delaunay"),
                                    name=self.name + "_del")

        VoronoiGeometry(project=self,
                        network=net,
                        pores=net.pores("voronoi"),
                        throats=net.throats("voronoi"),
                        name=self.name + "_vor")

        # Tidy up network
        h = net.check_network_health()
        if len(h["disconnected_pores"]) > 0:
            topotools.trim(network=net, pores=h["disconnected_pores"])
        # Copy the Delaunay throat diameters to the boundary pores
        Ps = net.pores(["delaunay", "boundary"], mode="xnor")
        map_Ps = del_geom.to_global(pores=Ps)
        all_Ts = net.find_neighbor_throats(pores=Ps, flatten=False)
        for i, Ts in enumerate(all_Ts):
            Ts = np.asarray(Ts)
            Ts = Ts[net["throat.delaunay"][Ts]]
            if len(Ts) > 0:
                map_Ts = del_geom.to_local(throats=Ts)
                td = del_geom["throat.diameter"][map_Ts]
                ta = del_geom["throat.cross_sectional_area"][map_Ts]
                del_geom["pore.diameter"][map_Ps[i]] = td
                del_geom["pore.area"][map_Ps[i]] = ta
        del_geom.regenerate_models(propnames=["throat.equivalent_area"])


class DelaunayGeometry(GenericGeometry):
    r"""
    Subclass of GenericGeometry for the pores connected by throats formed from
    the Delaunay tesselation.

    Parameters
    ----------
    name : str
        A unique name for the network
    """

    def __init__(self, network=None, **kwargs):
        super().__init__(network=network, **kwargs)
        if network is not None:
            # Set all the required models
            vertices = network.find_pore_hulls()
            p_coords = np.array(
                [network["pore.coords"][list(p)] for p in vertices], dtype=object
            )
            self["pore.vertices"] = p_coords
            vertices = network.find_throat_facets()
            t_coords = np.array(
                [network["pore.coords"][list(t)] for t in vertices], dtype=object
            )
            self["throat.vertices"] = t_coords

            self.in_hull_volume()
            self["throat.normal"] = self._t_normals()
            self["throat.centroid"] = self._centroids(verts=t_coords)
            self["pore.centroid"] = self._centroids(verts=p_coords)
            (
                self["pore.indiameter"],
                self["pore.incenter"],
            ) = self._indiameter_from_fibers()
            self._throat_props()
            topotools.trim_occluded_throats(network=network, mask=self.name)
            self["throat.volume"] = np.zeros(1, dtype=float)
            self["throat.length"] = np.ones(1, dtype=float)
            self["throat.length"] *= self.network.fiber_rad * 2
            cen_lens = self._throat_c2c()
            self["throat.c2c"] = np.sum(cen_lens, axis=1)
            cen_lens[cen_lens <= 0.0] = 1e-12
            self["throat.conduit_lengths.pore1"] = cen_lens[:, 0]
            self["throat.conduit_lengths.throat"] = cen_lens[:, 1]
            self["throat.conduit_lengths.pore2"] = cen_lens[:, 2]
            # Configurable Models
            mod = gm.throat_capillary_shape_factor.compactness
            self.add_model(propname="throat.shape_factor", model=mod)
            mod = gm.pore_size.equivalent_diameter
            self.add_model(propname="pore.diameter", model=mod)
            self.add_model(propname="pore.area",
                           model=gm.pore_cross_sectional_area.sphere,
                           pore_diameter="pore.diameter")
            mod = gm.throat_surface_area.extrusion
            self.add_model(propname="throat.surface_area", model=mod)
            self.regenerate_models()

    def _t_normals(self):
        r"""
        Update the throat normals from the voronoi vertices
        """
        verts = self["throat.vertices"]
        value = np.zeros([len(verts), 3])
        for i, _ in enumerate(verts):
            if len(np.unique(verts[i][:, 0])) == 1:
                verts_2d = np.vstack((verts[i][:, 1], verts[i][:, 2])).T
            elif len(np.unique(verts[i][:, 1])) == 1:
                verts_2d = np.vstack((verts[i][:, 0], verts[i][:, 2])).T
            else:
                verts_2d = np.vstack((verts[i][:, 0], verts[i][:, 1])).T
            hull = sptl.ConvexHull(verts_2d, qhull_options="QJ Pp")
            sorted_verts = verts[i][hull.vertices].astype(float)
            v1 = sorted_verts[-1] - sorted_verts[0]
            v2 = sorted_verts[1] - sorted_verts[0]
            value[i] = tr.unit_vector(np.cross(v1, v2))
        return value

    def _centroids(self, verts):
        r"""
        Function to calculate the centroid as the mean of a set of vertices.
        Used for pore and throat.
        """
        value = np.zeros([len(verts), 3])
        for i, i_verts in enumerate(verts):
            value[i] = np.mean(i_verts, axis=0)
        return value

    def _indiameter_from_fibers(self):
        r"""
        Calculate an indiameter by distance transforming sections of the
        fiber image. By definition the maximum value will be the largest radius
        of an inscribed sphere inside the fibrous hull
        """
        Np = self.num_pores()
        indiam = np.zeros(Np, dtype=float)
        incen = np.zeros([Np, 3], dtype=float)
        hull_pores = np.unique(self._hull_image)
        (Lx, Ly, Lz) = np.shape(self._hull_image)
        (indx, indy, indz) = np.indices([Lx, Ly, Lz])
        indx = indx.flatten()
        indy = indy.flatten()
        indz = indz.flatten()
        for i, pore in enumerate(hull_pores):
            logger.info("Processing pore: " + str(i) + " of " + str(len(hull_pores)))
            dt_pore = self._dt_image * (self._hull_image == pore)
            indiam[pore] = dt_pore.max() * 2
            max_ind = np.argmax(dt_pore)
            incen[pore, 0] = indx[max_ind]
            incen[pore, 1] = indy[max_ind]
            incen[pore, 2] = indz[max_ind]
        indiam *= self.network.resolution
        incen *= self.network.resolution
        return (indiam, incen)

    def _throat_c2c(self):
        r"""
        Calculate the center to center distance from centroid of pore1 to
        centroid of throat to centroid of pore2.
        """
        net = self.network
        Nt = net.num_throats()
        p_cen = net["pore.centroid"]
        t_cen = net["throat.centroid"]
        conns = net["throat.conns"]
        p1 = conns[:, 0]
        p2 = conns[:, 1]
        v1 = t_cen - p_cen[p1]
        v2 = t_cen - p_cen[p2]
        check_nan = ~np.any(np.isnan(v1 + v2), axis=1)
        value = np.ones([Nt, 3], dtype=float) * np.nan
        for i in range(Nt):
            if check_nan[i]:
                value[i, 0] = np.linalg.norm(v1[i]) - self.network.fiber_rad
                value[i, 1] = self.network.fiber_rad * 2
                value[i, 2] = np.linalg.norm(v2[i]) - self.network.fiber_rad
        return value[net.throats(self.name)]

    def _throat_props(self):
        r"""
        Use the Voronoi vertices and perform image analysis to obtain throat
        properties
        """
        from skimage.measure import regionprops
        from skimage.morphology import convex_hull_image

        offset = self.network.fiber_rad
        Nt = self.num_throats()
        centroid = np.zeros([Nt, 3])
        incenter = np.zeros([Nt, 3])
        area = np.zeros(Nt)
        perimeter = np.zeros(Nt)
        inradius = np.zeros(Nt)
        equiv_diameter = np.zeros(Nt)
        eroded_verts = np.ndarray(Nt, dtype=object)

        res = 200
        vertices = self["throat.vertices"]
        normals = self["throat.normal"]
        z_axis = [0, 0, 1]

        for i in self.throats("delaunay"):
            logger.info("Processing throat " + str(i + 1) + " of " + str(Nt))
            # For boundaries some facets will already be aligned with the axis
            # if this is the case a rotation is unnecessary
            angle = tr.angle_between_vectors(normals[i], z_axis)
            if angle == 0.0 or angle == np.pi:
                # We are already aligned
                rotate_facet = False
                facet = vertices[i]
            else:
                rotate_facet = True
                M = tr.rotation_matrix(
                    tr.angle_between_vectors(normals[i], z_axis),
                    tr.vector_product(normals[i], z_axis),
                )
                facet = np.dot(vertices[i], M[:3, :3].T)
            x = facet[:, 0]
            y = facet[:, 1]
            z = facet[:, 2]
            # Get points in 2d for image analysis
            pts = np.column_stack((x, y))
            # Translate points so min sits at the origin
            translation = [pts[:, 0].min(), pts[:, 1].min()]
            pts -= translation
            order = np.int(math.ceil(-np.log10(np.max(pts))))
            # Normalise and scale the points so that largest span equals the
            # resolution to save on memory and create clear image
            max_factor = np.max([pts[:, 0].max(), pts[:, 1].max()])
            f = res / max_factor
            # Scale the offset and define a structuring element with radius
            r = f * offset
            # Only proceed if r is less than half the span of the image"
            if r <= res / 2:
                pts *= f
                minp1 = pts[:, 0].min()
                minp2 = pts[:, 1].min()
                maxp1 = pts[:, 0].max()
                maxp2 = pts[:, 1].max()
                img = np.zeros(
                    [
                        np.int(math.ceil(maxp1 - minp1) + 1),
                        np.int(math.ceil(maxp2 - minp2) + 1),
                    ]
                )
                int_pts = np.around(pts.astype(float), 0).astype(int)
                for pt in int_pts:
                    img[pt[0]][pt[1]] = 1
                # Pad with zeros all the way around the edges
                img_pad = np.zeros([np.shape(img)[0] + 2, np.shape(img)[1] + 2])
                img_pad[1: np.shape(img)[0] + 1, 1: np.shape(img)[1] + 1] = img
                # All points should lie on this plane but could be some
                # rounding errors so use the order parameter
                z_plane = np.unique(np.around(z.astype(float), order + 1))
                if len(z_plane) > 1:
                    logger.error("Throat " + str(i) + " Rotation Failure")
                    temp_arr = np.ones(1)
                    temp_arr.fill(np.mean(z_plane))
                    z_plane = temp_arr
                # Fill in the convex hull polygon
                convhullimg = convex_hull_image(img_pad)
                # Perform a Distance Transform and black out points less than r
                # to create binary erosion. This is faster than performing an
                # erosion and dt can also be used later to find incircle
                eroded = ndimage.distance_transform_edt(convhullimg)
                eroded[eroded <= r] = 0
                eroded[eroded > r] = 1
                # If we are left with less than 3 non-zero points then the
                # throat is fully occluded
                if np.sum(eroded) >= 3:
                    # Do some image analysis to extract the key properties
                    cropped = eroded[1: np.shape(img)[0] + 1,
                                     1: np.shape(img)[1] + 1].astype(int)
                    regions = regionprops(cropped)
                    # Change this to cope with genuine multi-region throats
                    if len(regions) == 1:
                        for props in regions:
                            x0, y0 = props.centroid
                            equiv_diameter[i] = props.equivalent_diameter
                            area[i] = props.area
                            perimeter[i] = props.perimeter
                            coords = props.coords
                        # Undo the translation, scaling and truncation on the
                        # centroid
                        centroid2d = [x0, y0] / f
                        centroid2d += translation
                        centroid3d = np.concatenate((centroid2d, z_plane))
                        # Distance transform the eroded facet to find the
                        # incenter and inradius
                        dt = ndimage.distance_transform_edt(eroded)
                        temp = np.unravel_index(dt.argmax(), dt.shape)
                        inx0, iny0 = np.asarray(temp).astype(float)
                        incenter2d = [inx0, iny0]
                        # Undo the translation, scaling and truncation on the
                        # incenter
                        incenter2d /= f
                        incenter2d += translation
                        incenter3d = np.concatenate((incenter2d, z_plane))
                        # The offset vertices will be those in the coords that
                        # are closest to the originals
                        offset_verts = []
                        for pt in int_pts:
                            vert = np.argmin(np.sum(np.square(coords - pt), axis=1))
                            if vert not in offset_verts:
                                offset_verts.append(vert)
                        # If we are left with less than 3 different vertices
                        # then the throat is fully occluded as we can't make a
                        # shape with non-zero area
                        if len(offset_verts) >= 3:
                            offset_coords = coords[offset_verts].astype(float)
                            # Undo the translation, scaling and truncation on
                            # the offset_verts
                            offset_coords /= f
                            offset_coords_3d = np.vstack(
                                (
                                    offset_coords[:, 0] + translation[0],
                                    offset_coords[:, 1] + translation[1],
                                    np.ones(len(offset_verts)) * z_plane,
                                )
                            )
                            oc_3d = offset_coords_3d.T
                            # Get matrix to un-rotate the co-ordinates back to
                            # the original orientation if we rotated in the
                            # first place
                            if rotate_facet:
                                MI = tr.inverse_matrix(M)
                                # Unrotate the offset coordinates
                                incenter[i] = np.dot(incenter3d, MI[:3, :3].T)
                                centroid[i] = np.dot(centroid3d, MI[:3, :3].T)
                                eroded_verts[i] = np.dot(oc_3d, MI[:3, :3].T)
                            else:
                                incenter[i] = incenter3d
                                centroid[i] = centroid3d
                                eroded_verts[i] = oc_3d

                            inradius[i] = dt.max()
                            # Undo scaling on other parameters
                            area[i] /= f * f
                            perimeter[i] /= f
                            equiv_diameter[i] /= f
                            inradius[i] /= f
                        else:
                            area[i] = 0
                            perimeter[i] = 0
                            equiv_diameter[i] = 0

        self["throat.cross_sectional_area"] = area
        self["throat.perimeter"] = perimeter
        self["throat.centroid"] = centroid
        self["throat.diameter"] = equiv_diameter
        self["throat.indiameter"] = inradius * 2
        self["throat.incenter"] = incenter
        self["throat.offset_vertices"] = eroded_verts

    def inhull(self, xyz, pore, tol=1e-7):
        r"""
        Tests whether points lie within a convex hull or not.
        Computes a tesselation of the hull works out the normals of the facets.
        Then tests whether dot(x.normals) < dot(a.normals) where a is the the
        first vertex of the facets
        """
        xyz = np.around(xyz, 10)
        # Work out range to span over for pore hull
        xmin = xyz[:, 0].min()
        xr = (np.ceil(xyz[:, 0].max()) - np.floor(xmin)).astype(int) + 1
        ymin = xyz[:, 1].min()
        yr = (np.ceil(xyz[:, 1].max()) - np.floor(ymin)).astype(int) + 1
        zmin = xyz[:, 2].min()
        zr = (np.ceil(xyz[:, 2].max()) - np.floor(zmin)).astype(int) + 1

        origin = np.array([xmin, ymin, zmin])
        # start index
        si = np.floor(origin).astype(int)
        xyz -= origin
        dom = np.zeros([xr, yr, zr], dtype=np.uint8)
        indx, indy, indz = np.indices((xr, yr, zr))
        # Calculate the tesselation of the points
        hull = sptl.ConvexHull(xyz)
        # Assume 3d for now
        # Calc normals from the vector cross product of the vectors defined
        # by joining points in the simplices
        vab = xyz[hull.simplices[:, 0]] - xyz[hull.simplices[:, 1]]
        vac = xyz[hull.simplices[:, 0]] - xyz[hull.simplices[:, 2]]
        nrmls = np.cross(vab, vac)
        # Scale normal vectors to unit length
        nrmlen = np.sum(nrmls ** 2, axis=-1) ** (1.0 / 2)
        nrmls = nrmls * np.tile((1 / nrmlen), (3, 1)).T
        # Center of Mass
        center = np.mean(xyz, axis=0)
        # Any point from each simplex
        a = xyz[hull.simplices[:, 0]]
        # Make sure all normals point inwards
        dp = np.sum((np.tile(center, (len(a), 1)) - a) * nrmls, axis=-1)
        k = dp < 0
        nrmls[k] = -nrmls[k]
        # Now we want to test whether dot(x,N) >= dot(a,N)
        aN = np.sum(nrmls * a, axis=-1)
        for plane_index, _ in enumerate(a):
            eqx = nrmls[plane_index][0] * (indx)
            eqy = nrmls[plane_index][1] * (indy)
            eqz = nrmls[plane_index][2] * (indz)
            xN = eqx + eqy + eqz
            dom[xN - aN[plane_index] >= 0 - tol] += 1
        dom[dom < len(a)] = 0
        dom[dom == len(a)] = 1
        ds = np.shape(dom)
        temp_arr = np.zeros_like(self._hull_image, dtype=bool)
        temp_arr[si[0]: si[0] + ds[0],
                 si[1]: si[1] + ds[1],
                 si[2]: si[2] + ds[2]] = dom
        self._hull_image[temp_arr] = pore
        del temp_arr

    def in_hull_volume(self):
        r"""
        Work out the voxels inside the convex hull of the voronoi vertices of
        each pore
        """
        i = self.network["pore.internal"]
        s = self.network["pore.surface"]
        d = self.network["pore.delaunay"]
        Ps = self.network.pores()[np.logical_and(d, np.logical_or(i, s))]
        # Get the fiber image
        self._get_fiber_image()
        hull_image = np.ones_like(self._fiber_image, dtype=np.uint16) * -1
        self._hull_image = hull_image
        for pore in Ps:
            logger.info("Processing Pore: " + str(pore + 1) + " of " + str(len(Ps)))
            verts = self["pore.vertices"][pore]
            verts = np.asarray(unique_list(np.around(verts, 6)))
            verts /= self.network.resolution
            self.inhull(verts, pore)
        # Catch the voxels that escaped the hulls
        max_h = ndimage.maximum_filter(self._hull_image, size=2)
        mask = self._hull_image == -1
        self._hull_image[mask] = max_h[mask]
        self._process_pore_voxels()

    def _process_pore_voxels(self):
        r"""
        Function to count the number of voxels in the pore and fiber space
        Which are assigned to each hull volume
        """

        num_Ps = self.num_pores()
        pore_vox = np.zeros(num_Ps, dtype=int)
        fiber_vox = np.zeros(num_Ps, dtype=int)
        pore_space = self._hull_image.copy()
        fiber_space = self._hull_image.copy()
        pore_space[self._fiber_image == 0] = -1
        fiber_space[self._fiber_image == 1] = -1
        freq_pore_vox = np.transpose(np.unique(pore_space, return_counts=True))
        freq_pore_vox = freq_pore_vox[freq_pore_vox[:, 0] > -1]
        freq_fiber_vox = np.transpose(np.unique(fiber_space, return_counts=True))
        freq_fiber_vox = freq_fiber_vox[freq_fiber_vox[:, 0] > -1]
        pore_vox[freq_pore_vox[:, 0]] = freq_pore_vox[:, 1]
        fiber_vox[freq_fiber_vox[:, 0]] = freq_fiber_vox[:, 1]
        self["pore.volume"] = pore_vox * self.network.resolution ** 3
        del pore_space
        del fiber_space

    def _bresenham(self, faces, dx):
        r"""
        A Bresenham line function to generate points to fill in for the fibers
        """
        line_points = []
        for face in faces:
            # Get in hull order
            fx = face[:, 0]
            fy = face[:, 1]
            fz = face[:, 2]
            # Find the axis with the smallest spread and remove it to make 2D
            if (np.std(fx) < np.std(fy)) and (np.std(fx) < np.std(fz)):
                f2d = np.vstack((fy, fz)).T
            elif (np.std(fy) < np.std(fx)) and (np.std(fy) < np.std(fz)):
                f2d = np.vstack((fx, fz)).T
            else:
                f2d = np.vstack((fx, fy)).T
            hull = sptl.ConvexHull(f2d, qhull_options="QJ Pp")
            face = np.around(face[hull.vertices].astype(float), 6)
            for i, _ in enumerate(face):
                vec = face[i] - face[i - 1]
                vec_length = np.linalg.norm(vec)
                increments = np.int(np.ceil(vec_length / dx))
                check_p_old = np.array([-1, -1, -1])
                for x in np.linspace(0, 1, increments):
                    check_p_new = face[i - 1] + (vec * x)
                    if np.sum(check_p_new - check_p_old) != 0:
                        line_points.append(check_p_new)
                        check_p_old = check_p_new
        return np.asarray(line_points)

    def _get_fiber_image(self):
        r"""
        Produce image by filling in voxels along throat edges using Bresenham
        line then performing distance transform on fiber voxels to erode the
        pore space
        """
        fiber_rad = self.network.fiber_rad
        vox_len = self.network.resolution
        # Voxel length of fiber radius
        fiber_rad = np.around((fiber_rad - (vox_len / 2)) / vox_len, 0).astype(int)
        verts = self["throat.vertices"]
        [vxmin, vxmax, vymin, vymax, vzmin, vzmax] = self.vertex_dimension(
            face1=self.pores(), parm="minmax"
        )
        # Translate vertices so that minimum occurs at the origin
        for index, _ in enumerate(verts):
            verts[index] -= np.array([vxmin, vymin, vzmin])
        # Find new size of image array
        cdomain = np.around(
            np.array([(vxmax - vxmin), (vymax - vymin), (vzmax - vzmin)]), 6
        )
        logger.info("Creating fibers in range: " + str(np.around(cdomain, 5)))
        lx = np.int(np.around(cdomain[0] / vox_len) + 1)
        ly = np.int(np.around(cdomain[1] / vox_len) + 1)
        lz = np.int(np.around(cdomain[2] / vox_len) + 1)
        logger.info("Voxels: " + str(lx) + " " + str(ly) + " " + str(lz))
        # Try to create all the arrays we will need at total domain size
        try:
            pore_space = np.ones([lx, ly, lz], dtype=np.uint8)
            fiber_space = np.zeros(shape=[lx, ly, lz], dtype=np.uint8)
            dt = np.zeros([lx, ly, lz], dtype=float)
            # Only need one chunk
            cx = cy = cz = 1
            chunk_len = np.max(np.shape(pore_space))
        except Exception:
            logger.info(
                "Domain too large to fit into memory so chunking "
                + "domain to process image, this may take some time"
            )
            # Do chunking
            chunk_len = 100
            if lx > chunk_len:
                cx = np.ceil(lx / chunk_len).astype(int)
            else:
                cx = 1
            if ly > chunk_len:
                cy = np.ceil(ly / chunk_len).astype(int)
            else:
                cy = 1
            if lz > chunk_len:
                cz = np.ceil(lz / chunk_len).astype(int)
            else:
                cz = 1

        # Get image of the fibers
        line_points = self._bresenham(verts, vox_len / 2)
        line_ints = (np.around((line_points / vox_len), 0)).astype(int)
        for x, y, z in line_ints:
            try:
                pore_space[x][y][z] = 0
            except IndexError:
                logger.warning(
                    "Some elements in image processing are out" + "of bounds"
                )

        num_chunks = np.int(cx * cy * cz)
        cnum = 1
        for ci in range(cx):
            for cj in range(cy):
                for ck in range(cz):
                    # Work out chunk range
                    logger.info(
                        "Processing fiber Chunk: "
                        + str(cnum)
                        + " of "
                        + str(num_chunks)
                    )
                    cxmin = ci * chunk_len
                    cxmax = np.int(np.ceil((ci + 1) * chunk_len + 5 * fiber_rad))
                    cymin = cj * chunk_len
                    cymax = np.int(np.ceil((cj + 1) * chunk_len + 5 * fiber_rad))
                    czmin = ck * chunk_len
                    czmax = np.int(np.ceil((ck + 1) * chunk_len + 5 * fiber_rad))
                    # Don't overshoot
                    if cxmax > lx:
                        cxmax = lx
                    if cymax > ly:
                        cymax = ly
                    if czmax > lz:
                        czmax = lz
                    dt_edt = ndimage.distance_transform_edt
                    dtc = dt_edt(pore_space[cxmin:cxmax, cymin:cymax, czmin:czmax])
                    fiber_space[cxmin:cxmax, cymin:cymax, czmin:czmax][
                        dtc <= fiber_rad
                    ] = 0
                    fiber_space[cxmin:cxmax, cymin:cymax, czmin:czmax][
                        dtc > fiber_rad
                    ] = 1
                    dt[cxmin:cxmax, cymin:cymax, czmin:czmax] = dtc - fiber_rad
                    cnum += 1
        del pore_space
        self._fiber_image = fiber_space
        dt[dt < 0] = 0
        self._dt_image = dt

    def _get_fiber_slice(self, plane=None, index=None):
        r"""
        Plot an image of a slice through the fiber image
        plane contains percentage values of the length of the image in each
        axis

        Parameters
        ----------
        plane : array_like
        List of 3 values, [x,y,z], 2 must be zero and the other must be between
        zero and one representing the fraction of the domain to slice along
        the non-zero axis

        index : array_like
        similar to plane but instead of the fraction an index of the image is
        used
        """
        if hasattr(self, "_fiber_image") is False:
            logger.warning("This method only works when a fiber image exists")
            return None
        if plane is None and index is None:
            logger.warning("Please provide a plane array or index array")
            return None

        if plane is not None:
            if "array" not in plane.__class__.__name__:
                plane = np.asarray(plane)
            if np.sum(plane == 0) != 2:
                logger.warning(
                    "Plane argument must have two zero valued "
                    + "elements to produce a planar slice"
                )
                return None
            t = np.asarray(np.shape(self._fiber_image))
            s = np.around(plane * t).astype(int)
            s[s >= self._fiber_image.shape] -= 1
        elif index is not None:
            if "array" not in index.__class__.__name__:
                index = np.asarray(index)
            if np.sum(index == 0) != 2:
                logger.warning(
                    "Index argument must have two zero valued "
                    + "elements to produce a planar slice"
                )
                return None
            if "int" not in str(index.dtype):
                index = np.around(index).astype(int)
            s = index

        if s[0] != 0:
            slice_image = self._fiber_image[s[0], :, :]
        elif s[1] != 0:
            slice_image = self._fiber_image[:, s[1], :]
        else:
            slice_image = self._fiber_image[:, :, s[2]]

        return slice_image

    def plot_fiber_slice(self, plane=None, index=None, fig=None):
        r"""
        Plot one slice from the fiber image

        Parameters
        ----------
        plane : array_like
        List of 3 values, [x,y,z], 2 must be zero and the other must be between
        zero and one representing the fraction of the domain to slice along
        the non-zero axis

        index : array_like
        similar to plane but instead of the fraction an index of the image is
        used
        """
        import matplotlib.pyplot as plt

        if hasattr(self, "_fiber_image") is False:
            logger.warning("This method only works when a fiber image exists")
            return
        slice_image = self._get_fiber_slice(plane, index)
        if slice_image is not None:
            if fig is None:
                plt.figure()
            plt.imshow(
                slice_image.T, cmap="Greys", origin="lower", interpolation="nearest"
            )

        return fig

    def plot_porosity_profile(self, fig=None):
        r"""
        Return a porosity profile in all orthogonal directions by summing
        the voxel volumes in consectutive slices.
        """
        import matplotlib.pyplot as plt

        if hasattr(self, "_fiber_image") is False:
            logger.warning("This method only works when a fiber image exists")
            return

        im_shape = np.asarray(np.shape(self._fiber_image))
        px = np.zeros(im_shape[0])
        py = np.zeros(im_shape[1])
        pz = np.zeros(im_shape[2])

        for x in np.arange(im_shape[0]):
            px[x] = np.sum(self._fiber_image[x, :, :])
            px[x] /= np.size(self._fiber_image[x, :, :])
        for y in np.arange(im_shape[1]):
            py[y] = np.sum(self._fiber_image[:, y, :])
            py[y] /= np.size(self._fiber_image[:, y, :])
        for z in np.arange(im_shape[2]):
            pz[z] = np.sum(self._fiber_image[:, :, z])
            pz[z] /= np.size(self._fiber_image[:, :, z])

        if fig is None:
            fig = plt.figure()
        ax = fig.gca()
        plots = []
        plots.append(plt.plot(np.arange(im_shape[0]) / im_shape[0],
                              px, "r", label="x"))
        plots.append(plt.plot(np.arange(im_shape[1]) / im_shape[1],
                              py, "g", label="y"))
        plots.append(plt.plot(np.arange(im_shape[2]) / im_shape[2],
                              pz, "b", label="z"))
        plt.xlabel("Normalized Distance")
        plt.ylabel("Porosity")
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles, labels, loc=1)
        plt.legend(bbox_to_anchor=(1, 1), loc=2, borderaxespad=0.0)
        return fig

    def plot_throat(self, throats, fig=None):
        r"""
        Plot a throat or list of throats in 2D showing key data

        Parameters
        ----------
        throats : list or array containing throat indices tp include in figure

        fig : matplotlib figure object to place plot in
        """
        import matplotlib.pyplot as plt

        throat_list = []
        for throat in throats:
            if throat in range(self.num_throats()):
                throat_list.append(throat)
            else:
                logger.warn("Throat: " + str(throat) + " not part of geometry")
        if len(throat_list) > 0:
            verts = self["throat.vertices"][throat_list]
            offsets = self["throat.offset_vertices"][throat_list]
            normals = self["throat.normal"][throat_list]
            coms = self["throat.centroid"][throat_list]
            incentre = self["throat.incenter"][throat_list]
            inradius = 0.5 * self["throat.indiameter"][throat_list]
            row_col = int(np.ceil(np.sqrt(len(throat_list))))
            for i, _ in enumerate(throat_list):
                if fig is None:
                    fig = plt.figure()
                ax = fig.add_subplot(row_col, row_col, i + 1)
                vert_2D = self._rotate_and_chop(verts[i], normals[i], [0, 0, 1])
                hull = ConvexHull(vert_2D, qhull_options="QJ Pp")
                for simplex in hull.simplices:
                    plt.plot(
                        vert_2D[simplex, 0], vert_2D[simplex, 1], "k-", linewidth=2
                    )
                plt.scatter(vert_2D[:, 0], vert_2D[:, 1])
                offset_2D = self._rotate_and_chop(offsets[i], normals[i], [0, 0, 1])
                offset_hull = ConvexHull(offset_2D, qhull_options="QJ Pp")
                for simplex in offset_hull.simplices:
                    plt.plot(offset_2D[simplex, 0],
                             offset_2D[simplex, 1],
                             "g-", linewidth=2)
                plt.scatter(offset_2D[:, 0], offset_2D[:, 1])
                # Make sure the plot looks nice by finding the greatest
                # range of points and setting the plot to look square
                xmax = vert_2D[:, 0].max()
                xmin = vert_2D[:, 0].min()
                ymax = vert_2D[:, 1].max()
                ymin = vert_2D[:, 1].min()
                x_range = xmax - xmin
                y_range = ymax - ymin
                if x_range > y_range:
                    my_range = x_range
                else:
                    my_range = y_range
                lower_bound_x = xmin - my_range * 0.5
                upper_bound_x = xmin + my_range * 1.5
                lower_bound_y = ymin - my_range * 0.5
                upper_bound_y = ymin + my_range * 1.5
                plt.axis((lower_bound_x, upper_bound_x,
                          lower_bound_y, upper_bound_y))
                plt.grid(b=True, which="major", color="b", linestyle="-")
                centroid = self._rotate_and_chop(coms[i], normals[i], [0, 0, 1])
                incent = self._rotate_and_chop(incentre[i], normals[i], [0, 0, 1])
                plt.scatter(centroid[0][0], centroid[0][1])
                plt.scatter(incent[0][0], incent[0][1], c="r")
                # Plot incircle
                t = np.linspace(0, 2 * np.pi, 200)
                u = inradius[i] * np.cos(t) + incent[0][0]
                v = inradius[i] * np.sin(t) + incent[0][1]
                plt.plot(u, v, "r-")
                ax.ticklabel_format(style="sci", scilimits=(0, 0))
        else:
            logger.error("Please provide throat indices")
        return fig

    def plot_pore(self, pores, fig=None, axis_bounds=None, include_points=False):
        r"""
        Plot all throats around a given pore or list of pores in 3D

        Parameters
        ----------
        pores : list or array containing pore indices tp include in figure

        fig : matplotlib figure object to place plot in

        axis_bounds : list of [xmin, xmax, ymin, ymax, zmin, zmax] values
            to limit axes to

        include_points : bool
            Determines whether to scatter pore and throat centroids
        """
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection

        if len(pores) > 0:
            net = self.network
            net_pores = self.to_global(pores=pores)
            centroids = self["pore.centroid"][pores]
            coords = net["pore.coords"][net_pores]
            net_throats = net.find_neighbor_throats(pores=net_pores)
            throats = self.to_local(throats=net_throats)
            tcentroids = self["throat.centroid"][throats]
            # Can't create volume from one throat
            if 1 <= len(throats):
                verts = self["throat.vertices"][throats]
                normals = self["throat.normal"][throats]
                # Get verts in hull order
                ordered_verts = []
                for i, _ in enumerate(verts):
                    vert_2D = self._rotate_and_chop(verts[i], normals[i], [0, 0, 1])
                    hull = ConvexHull(vert_2D, qhull_options="QJ Pp")
                    ordered_verts.append(verts[i][hull.vertices])
                offsets = self["throat.offset_vertices"][throats]
                ordered_offs = []
                for i, _ in enumerate(offsets):
                    offs_2D = self._rotate_and_chop(offsets[i],
                                                    normals[i],
                                                    [0, 0, 1])
                    offs_hull = ConvexHull(offs_2D, qhull_options="QJ Pp")
                    ordered_offs.append(offsets[i][offs_hull.vertices])
                # Get domain extents for setting axis
                if axis_bounds is None:
                    [xmin, xmax, ymin, ymax, zmin, zmax] = self.vertex_dimension(
                        net_pores, parm="minmax"
                    )
                else:
                    [xmin, xmax, ymin, ymax, zmin, zmax] = axis_bounds
                if fig is None:
                    fig = plt.figure()
                ax = fig.gca(projection="3d")
                outer_items = Poly3DCollection(
                    ordered_verts, linewidths=1, alpha=0.2, zsort="min"
                )
                outer_face_colours = [(1, 0, 0, 0.01)]
                outer_items.set_facecolor(outer_face_colours)
                ax.add_collection(outer_items)
                inner_items = Poly3DCollection(
                    ordered_offs, linewidths=1, alpha=0.2, zsort="min"
                )
                inner_face_colours = [(0, 0, 1, 0.01)]
                inner_items.set_facecolor(inner_face_colours)
                ax.add_collection(inner_items)
                ax.set_xlim(xmin, xmax)
                ax.set_ylim(ymin, ymax)
                ax.set_zlim(zmin, zmax)
                if include_points:
                    ax.scatter(centroids[:, 0],
                               centroids[:, 1],
                               centroids[:, 2],
                               c="y")
                    ax.scatter(tcentroids[:, 0],
                               tcentroids[:, 1],
                               tcentroids[:, 2],
                               c="r")
                    ax.scatter(coords[:, 0], coords[:, 1], coords[:, 2], c="b")
                ax.ticklabel_format(style="sci", scilimits=(0, 0))
            else:
                self.plot_throat(throats, fig)
        else:
            logger.error("Please provide pore indices")
        return fig

    def _rotate_and_chop(self, verts, normal, axis=[0, 0, 1]):
        r"""
        Method to rotate a set of vertices (or coords) to align with an axis
        points must be coplanar and normal must be given
        Chops axis coord to give vertices back in 2D
        Used to prepare verts for printing or calculating convex hull in order
        to arrange them in hull order for calculations and printing
        """
        xaxis = [1, 0, 0]
        yaxis = [0, 1, 0]
        zaxis = [0, 0, 1]
        angle = tr.angle_between_vectors(normal, axis)
        if angle == 0.0 or angle == np.pi:
            # We are already aligned
            facet = verts
        else:
            M = tr.rotation_matrix(tr.angle_between_vectors(normal, axis),
                                   tr.vector_product(normal, axis))
            try:
                facet = np.dot(verts, M[:3, :3].T)
            except ValueError:
                pass
        try:
            x = facet[:, 0]
            y = facet[:, 1]
            z = facet[:, 2]
        except IndexError:
            x = facet[0]
            y = facet[1]
            z = facet[2]
        # Work out span of points and set axes scales to cover this and be
        # equal in both dimensions
        if axis == xaxis:
            output = np.column_stack((y, z))
        elif axis == yaxis:
            output = np.column_stack((x, z))
        elif axis == zaxis:
            output = np.column_stack((x, y))
        else:
            output = facet
        return output

    def vertex_dimension(self, face1=[], face2=[], parm="volume"):
        r"""
        Return the domain extent based on the vertices

        This function is better than using the pore coords as they may be far
        away from the original domain size.  And will alter the effective
        properties which should be based on the original domain sizes. Takes
        one or two sets of pores and works out different geometric properties
        if "length" is specified and two lists are given the planarity is
        determined and the appropriate length (x,y,z) is returned.

        Parameters
        ----------
        face1 : list or array containing pore indices for a face to include in
            calculations.

        parm : str
            Determines what information is returned:
                volume, area (_xy, _xz, _yz), length (_x, _y, _z), minmax.
            Default volume.

        """
        prj = self.project
        network = prj.network
        pores = np.array([], dtype=int)
        if 0 < len(face1):
            pores = np.hstack((pores, face1))
        if 0 < len(face2):
            pores = np.hstack((pores, face2))
        face1_coords = np.around(network["pore.coords"][face1], 12)
        face2_coords = np.around(network["pore.coords"][face2], 12)
        face1_planar = np.zeros(3)
        face2_planar = np.zeros(3)
        planar = np.zeros(3)
        for i in range(3):
            if len(np.unique(face1_coords[:, i])) == 1:
                face1_planar[i] = 1
            if len(np.unique(face2_coords[:, i])) == 1:
                face2_planar[i] = 1
        if 0 < len(face1) and 0 < len(face2):
            planar = face1_planar * face2_planar
        elif 0 < len(face1):
            planar = face1_planar
        elif 0 < len(face2):
            planar = face2_planar
        else:
            return 0
        verts = []
        for pore in pores:
            for vert in np.asarray(list(self["pore.vertices"][pore])):
                verts.append(vert)
        verts = np.asarray(verts)

        vx_min = verts[:, 0].min()
        vx_max = verts[:, 0].max()
        vy_min = verts[:, 1].min()
        vy_max = verts[:, 1].max()
        vz_min = verts[:, 2].min()
        vz_max = verts[:, 2].max()
        output = 0
        width = np.around(vx_max - vx_min, 10)
        depth = np.around(vy_max - vy_min, 10)
        height = np.around(vz_max - vz_min, 10)
        if parm == "volume":
            output = width * depth * height
        elif parm == "area_xy" or (parm == "area" and planar[2] == 1):
            output = width * depth
        elif parm == "area_xz" or (parm == "area" and planar[1] == 1):
            output = width * height
        elif parm == "area_yz" or (parm == "area" and planar[0] == 1):
            output = depth * height
        elif parm == "length_x" or (parm == "length" and planar[0] == 1):
            output = width
        elif parm == "length_y" or (parm == "length" and planar[1] == 1):
            output = depth
        elif parm == "length_z" or (parm == "length" and planar[2] == 1):
            output = height
        elif parm == "minmax":
            output = [vx_min, vx_max, vy_min, vy_max, vz_min, vz_max]
        return output


class VoronoiGeometry(GenericGeometry):
    r"""
    Subclass of GenericGeometry for the fibers making up the complimentary
    solid network.

    Parameters
    ----------
    name : str
        A unique name for the network
    """

    def __init__(self, network=None, **kwargs):
        super().__init__(network=network, **kwargs)
        if network is not None:
            rm = "normal"
            net_Ps = network.pores(self.name)
            self["pore.diameter"] = np.ones(self.Np) * network.fiber_rad * 2
            self["pore.indiameter"] = self["pore.diameter"]
            self["pore.incenter"] = network["pore.coords"][net_Ps]
            self["pore.centroid"] = network["pore.coords"][net_Ps]
            self._throat_props()
            self.add_model(
                propname="pore.volume", model=gm.pore_volume.sphere, regen_mode=rm
            )
            self.add_model(propname="pore.area",
                           model=gm.pore_cross_sectional_area.sphere,
                           regen_mode=rm)
            self["throat.diameter"] = np.ones(self.Nt) * network.fiber_rad * 2
            self["throat.indiameter"] = self["throat.diameter"]
            self.add_model(propname="throat.cross_sectional_area",
                           model=gm.throat_cross_sectional_area.cylinder,
                           regen_mode=rm)
            self.add_model(propname="throat.length",
                           model=gm.throat_length.spheres_and_cylinders,
                           regen_mode=rm)
            self["throat.c2c"] = self["throat.length"] + network.fiber_rad * 2
            self.add_model(propname="throat.volume",
                           model=gm.throat_volume.cylinder,
                           regen_mode=rm)
            self.add_model(propname="throat.perimeter",
                           model=gm.throat_perimeter.cylinder,
                           regen_mode=rm)
            self.add_model(propname="throat.surface_area",
                           model=gm.throat_surface_area.cylinder,
                           regen_mode=rm)
            self.add_model(propname="throat.shape_factor",
                           model=gm.throat_capillary_shape_factor.compactness,
                           regen_mode=rm)

    def _throat_props(self):
        r"""
        Helper Function to calculate the throat normal vectors
        """
        network = self.network
        net_Ts = network.throats(self.name)
        conns = network["throat.conns"][net_Ts]
        p1 = conns[:, 0]
        p2 = conns[:, 1]
        coords = network["pore.coords"]
        normals = tr.unit_vector(coords[p2] - coords[p1])
        self["throat.normal"] = normals
        self["throat.centroid"] = (coords[p1] + coords[p2]) / 2
        self["throat.incenter"] = self["throat.centroid"]
