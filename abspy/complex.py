"""
complex.py
----------

Cell complex from planar primitive arrangement.

A linear cell complex is constructed from planar primitives
with adaptive binary space partitioning: upon insertion of a primitive
only the local cells that are intersecting it will be updated,
so will be the corresponding adjacency graph of the complex.
"""

import os, sys
import string
from pathlib import Path
import itertools
import heapq
from copy import  deepcopy
from copy import copy
from random import random, choices, uniform
import pickle
import time
import multiprocessing
from fractions import Fraction
from copy import deepcopy
import numpy as np
from tqdm import trange
import networkx as nx
import trimesh
from sage.all import polytopes, QQ, RR, Polyhedron, vector, PolyhedralComplex
from treelib import Tree
import open3d as o3d

from .logger import attach_to_log
logger = attach_to_log()

from .export_complex import CellComplexExporter

sys.path.append("/home/rsulzer/cpp/compact_mesh_reconstruction/build/release/Benchmark/PyLabeler")
import libPyLabeler as PL

PYTHONPATH="/home/rsulzer/python"
sys.path.append(os.path.join(PYTHONPATH,"pyRANSAC-3D"))
from export import PlaneExporter

class CellComplex:
    """
    Class of cell complex from planar primitive arrangement.
    """
    def __init__(self, model, planes, halfspaces, bounds, points=None, initial_bound=None, initial_padding=0.1, additional_planes=None,
                 build_graph=False, quiet=False):
        """
        Init CellComplex.
        Class of cell complex from planar primitive arrangement.

        Parameters
        ----------
        planes: (n, 4) float
            Plana parameters
        bounds: (n, 2, 3) float
            Corresponding bounding box bounds of the planar primitives
        points: (n, ) object of float
            Points grouped into primitives, points[any]: (m, 3)
        initial_bound: None or (2, 3) float
            Initial bound to partition
        build_graph: bool
            Build the cell adjacency graph if set True.
        additional_planes: None or (n, 4) float
            Additional planes to append to the complex,
            can be missing planes due to occlusion or incapacity of RANSAC
        quiet: bool
            Disable logging and progress bar if set True
        """
        self.model = model
        self.planeExporter = PlaneExporter()
        self.cellComplexExporter = CellComplexExporter(self)


        self.quiet = quiet
        if self.quiet:
            logger.disabled = True

        logger.debug('Init cell complex with padding {}'.format(initial_padding))

        self.bounds = bounds  # numpy.array over RDF
        self.planes = planes  # numpy.array over RDF
        self.halfspaces = halfspaces
        self.points = points

        # missing planes due to occlusion or incapacity of RANSAC
        self.additional_planes = additional_planes

        if build_graph:
            self.graph = nx.Graph()
            self.graph.add_node(0)  # the initial cell
            self.index_node = 0  # unique for every cell ever generated
        else:
            self.graph = None

        self.constructed = False
        self.polygons_initialized = False
        self._init_bounding_box(model)


    def _init_bounding_box(self,m,scale=1.2):

        self.bounding_verts = []
        # points = np.load(m["pointcloud"])["points"]
        points = np.load(m["occ"])["points"]

        ppmin = points.min(axis=0)
        ppmax = points.max(axis=0)

        # ppmin = [-40,-40,-40]
        # ppmax = [40,40,40]

        pmin=[]
        for p in ppmin:
            pmin.append(Fraction(str(p)))
        pmax=[]
        for p in ppmax:
            pmax.append(Fraction(str(p)))

        self.bounding_verts.append(pmin)
        self.bounding_verts.append([pmin[0],pmax[1],pmin[2]])
        self.bounding_verts.append([pmin[0],pmin[1],pmax[2]])
        self.bounding_verts.append([pmin[0],pmax[1],pmax[2]])
        self.bounding_verts.append(pmax)
        self.bounding_verts.append([pmax[0],pmin[1],pmax[2]])
        self.bounding_verts.append([pmax[0],pmax[1],pmin[2]])
        self.bounding_verts.append([pmax[0],pmin[1],pmin[2]])

        self.bounding_poly = Polyhedron(vertices=self.bounding_verts)

    def _project_points_to_plane(self,points,plane):

        ### project inlier points to plane
        ## https://www.baeldung.com/cs/3d-point-2d-plane
        k = (-plane[-1] - plane[0] * points[:, 0] - plane[1] * points[:, 1] - plane[2] * points[:, 2]) / (plane[0] ** 2 + plane[1] ** 2 + plane[2] ** 2)
        pp = np.asarray([points[:, 0] + k * plane[0], points[:, 1] + k * plane[1], points[:, 2] + k * plane[2]])
        ## make e1 and e2 (see bottom of page linked above)
        ## take a starting vector (e0) and take a component of this vector which is nonzero (see here: https://stackoverflow.com/a/33758795)
        z = np.argmax(np.abs(plane[:3]))
        y = (z+1)%3
        x = (y+1)%3
        e0 = np.array(plane[:3])
        e0 = e0/np.linalg.norm(e0)
        e1 = np.zeros(3)
        ## reverse the non-zero component and put it on a different axis
        e1[x], e1[y], e1[z] = e0[x], -e0[z], e0[y]
        ## take the cross product of e0 and e1 to make e2
        e2 = np.cross(e0,e1)
        e12 = np.array([e1,e2])
        return (e12@pp).transpose()

    def _sorted_vertex_indices(self,adjacency_matrix):
        """
        Return sorted vertex indices.

        Parameters
        ----------
        adjacency_matrix: matrix
            Adjacency matrix

        Returns
        -------
        sorted_: list of int
            Sorted vertex indices
        """
        pointer = 0
        sorted_ = [pointer]
        for _ in range(len(adjacency_matrix[0]) - 1):
            connected = np.where(adjacency_matrix[pointer])[0]  # two elements
            if connected[0] not in sorted_:
                pointer = connected[0]
                sorted_.append(connected[0])
            else:
                pointer = connected[1]
                sorted_.append(connected[1])
        return sorted_

    def _sort_vertex_indices_by_angle(self,points,plane):
        '''order vertices of a convex polygon:
        https://blogs.sas.com/content/iml/2021/11/17/order-vertices-convex-polygon.html#:~:text=Order%20vertices%20of%20a%20convex%20polygon&text=You%20can%20use%20the%20centroid,vertices%20of%20the%20convex%20polygon
        '''
        ## project to plane
        pp=self._project_points_to_plane(points,plane)

        center = np.mean(pp,axis=0)
        vectors = pp[:,:2] - center[:2]
        # vectors = vectors/np.linalg.norm(vectors)
        radians = np.arctan2(vectors[:,1],vectors[:,0])

        same_rads = radians[np.unique(radians,return_counts=True)[1]>1]
        if same_rads.shape[0]:
            print("WARNING: same angle")
            return None

        return np.argsort(radians)

    def _orient_exact_polygon(self, points, outside):
        # check for left or right orientation
        # https://math.stackexchange.com/questions/2675132/how-do-i-determine-whether-the-orientation-of-a-basis-is-positive-or-negative-us

        i = 0
        cross=0
        while np.sum(cross) == 0:
            a = vector(points[i+1] - points[i])
            # a = a/a.norm()
            b = vector(points[i+2] - points[i])
            # b = b/b.norm()
            cross = a.cross_product(b)
            i+=1

        c = vector(np.array(outside,dtype=object) - points[i])
        # c = c/c.norm()
        # cross = cross/cross.norm()
        dot = cross.dot_product(c)

        return dot < 0

    def _orient_inexact_polygon(self, points, outside):
        # check for left or right orientation
        # https://math.stackexchange.com/questions/2675132/how-do-i-determine-whether-the-orientation-of-a-basis-is-positive-or-negative-us

        i = 0
        cross=0
        while np.sum(cross) == 0:
            a = points[i+1] - points[i]
            a = a/np.linalg.norm(a)
            b = points[i+2] - points[i]
            b = b/np.linalg.norm(b)
            cross = np.cross(a,b)
            i+=1

        c = np.array(outside) - points[i]
        c = c/np.linalg.norm(c)
        cross = cross/np.linalg.norm(cross)
        dot = np.dot(cross,c)

        return dot < 0

    def _get_intersection(self, e0, e1):

        if "vertices" in self.graph[e0][e1] and self.graph[e0][e1]["vertices"] is not None:
            pts = []
            for v in self.graph[e0][e1]["vertices"]:
                pts.append(tuple(v))
            pts = list(set(pts))
            intersection_points = np.array(pts, dtype=object)
        elif "intersection" in self.graph[e0][e1] and self.graph[e0][e1] is not None:
            intersection_points = np.array(self.graph[e0][e1]["intersection"].vertices_list(), dtype=object)
        else:
            c0 = self.graph.nodes[e0]["convex"]
            c1 = self.graph.nodes[e1]["convex"]
            intersection = c0.intersection(c1)
            assert(intersection.dim()==2)
            intersection_points = np.array(intersection.vertices_list(), dtype=object)

        return intersection_points


    def extract_soup(self, filename):

        faces = []
        all_points = []
        n_points=0

        facet_lens = []

        for ec, (e0, e1) in enumerate(self.graph.edges):

            # if e0 > e1:
            #     continue

            c0 = self.graph.nodes[e0]
            c1 = self.graph.nodes[e1]

            if c0["occupancy"] != c1["occupancy"]:

                intersection_points = self._get_intersection(e0,e1)

                correct_order = self._sort_vertex_indices_by_angle(intersection_points.astype(float),self.graph[e0][e1]["supporting_plane"])
                assert(len(intersection_points)==len(correct_order))
                intersection_points = intersection_points[correct_order]

                if(len(intersection_points)<3):
                    continue

                ## orient polygon
                outside = c0["convex"].center() if c1["occupancy"] else c1["convex"].center()
                if self._orient_inexact_polygon(intersection_points,outside):
                    intersection_points = np.flip(intersection_points, axis=0)

                for i in range(intersection_points.shape[0]):
                    all_points.append(tuple(intersection_points[i,:]))
                faces.append(np.arange(len(intersection_points))+n_points)
                facet_lens.append(len(intersection_points))
                n_points+=len(intersection_points)


        # sys.path.append("/home/rsulzer/cpp/compact_mesh_reconstruction/build/release/Benchmark/Soup2Mesh")
        # import libSoup2Mesh as s2m
        # sm = s2m.Soup2Mesh()
        # sm.loadSoup(np.array(all_points,dtype=float), np.array(facet_lens,dtype=int), np.concatenate(faces,dtype=int))
        # triangulate = False
        # sm.makeMesh(triangulate)
        # sm.saveMesh(filename)

        logger.debug('Save polygon mesh to {}'.format(filename))
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        self.cellComplexExporter.write_off(filename,points=np.array(all_points,dtype=float),facets=faces)



    def extract_surface(self, filename):

        tris = []
        all_points = []
        for e0, e1 in self.graph.edges:

            # if e0 > e1:
            #     continue

            c0 = self.graph.nodes[e0]
            c1 = self.graph.nodes[e1]

            if c0["occupancy"] != c1["occupancy"]:


                intersection_points = self._get_intersection(e0,e1)

                intersection_points_float = intersection_points.astype(float)
                correct_order = self._sort_vertex_indices_by_angle(intersection_points_float,self.graph[e0][e1]["supporting_plane"])
                assert(len(intersection_points)==len(correct_order))
                intersection_points = intersection_points[correct_order]

                if(len(intersection_points)<3):
                    print("ERROR: Encountered facet with less than 2 vertices.")
                    sys.exit(1)

                ## orient polygon
                outside = c0["convex"].center() if c1["occupancy"] else c1["convex"].center()
                # if self._orient_inexact_polygon(intersection_points_float,np.array(outside).astype(float)):
                if self._orient_exact_polygon(intersection_points,outside):
                    intersection_points = np.flip(intersection_points, axis=0)

                for i in range(intersection_points.shape[0]):
                    all_points.append(tuple(intersection_points[i,:]))
                tris.append(intersection_points)

        pset = set(all_points)
        pset = np.array(list(pset),dtype=object)
        facets = []
        for tri in tris:
            face = []
            for pt in tri:
                # face.append(np.argwhere((pset == p).all(-1))[0][0])
                face.append(np.argwhere((np.equal(pset,pt,dtype=object)).all(-1))[0][0])

                # face.append(np.argwhere(np.isin(pset, p).all(-1))[0][0])
                # face.append(np.argwhere(np.isclose(pset, p,atol=tol*1.01).all(-1))[0][0])
            facets.append(face)


        logger.debug('Save polygon mesh to {}'.format(filename))
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        self.cellComplexExporter.write_off(filename,points=np.array(pset,dtype=float),facets=facets)

    def extract_in_out_cells(self):
        for i,node in enumerate(self.graph.nodes(data=True)):
            col = [1,0,0] if node[1]["occupancy"] == 1 else [0,0,1]
            self.cellComplexExporter.write_cells(self.model,node[1]['convex'],count=i,subfolder="in_out_cells",color=np.array(col))




    def extract_in_cells(self,filename,to_ply=True):


        os.makedirs(os.path.dirname(filename),exist_ok=True)
        f = open(filename,'w')

        def filter_node(node_id):
            return self.graph.nodes[node_id]["occupancy"]


        verts = []
        facets = []
        vert_count = 0
        view = nx.subgraph_view(self.graph,filter_node=filter_node)
        # for node in enumerate(self.graph.nodes(data=True)):
            # if node[1]["occupancy"] == 1:
        for node in view.nodes():
            c = np.random.random(size=3)
            c = (c * 255).astype(int)
            polyhedron = self.graph.nodes[node]["convex"]
            ss = polyhedron.render_solid().obj_repr(polyhedron.render_solid().default_render_params())
            for v in ss[2]:
                v = v.split(' ')
                verts.append([v[1], v[2], v[3], str(c[0]), str(c[1]), str(c[2])])

            for fa in ss[3]:
                tf = []
                for ffa in fa[2:].split(' '):
                    tf.append(str(int(ffa) + vert_count -1) + " ")
                facets.append(tf)
            vert_count+=len(ss[2])

        f.write("ply\n")
        f.write("format ascii 1.0\n")
        f.write("comment : in_cells:{}\n".format(len(view.nodes)))
        f.write("element vertex {}\n".format(len(verts)))
        f.write("property float x\n")
        f.write("property float y\n")
        f.write("property float z\n")
        f.write("property uchar red\n")
        f.write("property uchar green\n")
        f.write("property uchar blue\n")
        f.write("element face {}\n".format(len(facets)))
        f.write("property list uchar int vertex_index\n")
        f.write("end_header\n")
        for v in verts:
            f.write("{} {} {} {} {} {}\n".format(v[0],v[1],v[2],v[3],v[4],v[5]))
        for fa in facets:
            f.write("{} ".format(len(fa)))
            for v in fa:
                f.write("{}".format(v))
            f.write("\n")


        f.close()




    @staticmethod
    def _obj_str(cells, use_mtl=False, filename_mtl='colours.mtl'):
        """
        Convert a list of cells into a string of obj format.

        Parameters
        ----------
        cells: list of Polyhedra objects
            Polyhedra cells
        use_mtl: bool
            Use mtl attribute in obj if set True
        filename_mtl: None or str
            Material filename

        Returns
        -------
        scene_str: str
            String representation of the object
        material_str: str
            String representation of the material
        """
        scene = None
        for cell in cells:
            scene += cell.render_solid()

        # directly save the obj string from scene.obj() will bring the inverted facets
        scene_obj = scene.obj_repr(scene.default_render_params())
        if len(cells) == 1:
            scene_obj = [scene_obj]
        scene_str = ''
        material_str = ''

        if use_mtl:
            scene_str += f'mtllib {filename_mtl}\n'

        for o in range(len(cells)):
            scene_str += scene_obj[o][0] + '\n'

            if use_mtl:
                scene_str += scene_obj[o][1] + '\n'
                material_str += 'newmtl ' + scene_obj[o][1].split()[1] + '\n'
                material_str += 'Kd {:.3f} {:.3f} {:.3f}\n'.format(random(), random(), random())  # diffuse colour

            scene_str += '\n'.join(scene_obj[o][2]) + '\n'
            scene_str += '\n'.join(scene_obj[o][3]) + '\n'  # contents[o][4] are the interior facets
        return scene_str, material_str

    def extract_partition(self, filepath, indices_cells=None, use_mtl=False):
        """
        Save polygon soup of indexed convexes to an obj file.

        Parameters
        ----------
        filepath: str or Path
            Filepath to save obj file
        indices_cells: (n,) int
            Indices of cells to save to file
        use_mtl: bool
            Use mtl attribute in obj if set True
        """
        # create the dir if not exists
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        cells = [self.cells[i] for i in indices_cells] if indices_cells is not None else self.cells
        scene_str, material_str = self._obj_str(cells, use_mtl=use_mtl, filename_mtl=f'{filepath.stem}.mtl')

        with open(filepath, 'w') as f:
            f.writelines("# cells: {}\n".format(len(self.cells)))
            f.writelines(scene_str)
        if use_mtl:
            with open(filepath.with_name(f'{filepath.stem}.mtl'), 'w') as f:
                f.writelines(material_str)


    def label_cells(self, m, n_test_points=50,export=False):

        pl=PL.PyLabeler(n_test_points)
        pl.loadMesh(m["mesh"])
        points = []
        points_len = []
        # for i,node in enumerate(self.graph.nodes(data=True)):
        #     cell = node[1]['convex']
        for i,cell in enumerate(self.cells):
            if export:
                self.cellComplexExporter.write_cells(m,cell,count=i,subfolder="final_cells")
            pts = np.array(cell.vertices())
            points.append(pts)
            # print(pts)
            points_len.append(pts.shape[0])


        # assert(isinstance(points[0].dtype,np.float32))
        occs = pl.labelCells(np.array(points_len),np.concatenate(points,axis=0))
        del pl

        foccs = dict(zip(self.graph.nodes, occs))
        nx.set_node_attributes(self.graph,occs,"float_occupancy")

        occs = dict(zip(self.graph.nodes, np.rint(occs).astype(int)))
        nx.set_node_attributes(self.graph,occs,"occupancy")


    def _get_best_plane(self,current_ids,planes,point_groups,export=False):

        ### pad the point groups with NaNs to make a numpy array from the variable lenght list
        ### could maybe better be done with scipy sparse, but would require to rewrite the _get and _split functions used below

        ### the whole thing vectorized. doesn't really work for some reason
        # UPDATE: should work, first tries where with wrong condition
        # planes = np.repeat(vertex_group.planes[np.newaxis,current_ids,:],current_ids.shape[0],axis=0)
        # pgs = np.repeat(point_groups[np.newaxis,current_ids,:,:],current_ids.shape[0],axis=0)
        #
        # which_side = planes[:,:,0,np.newaxis] * pgs[:,:,:,0] + planes[:,:,1,np.newaxis] * pgs[:,:,:,1] + planes[:,:,2,np.newaxis] * pgs[:,:,:,2] + planes[:,:,3,np.newaxis]

        ### find the plane which seperates all other planes without splitting them
        left_right = []
        for i,id in enumerate(current_ids):
            left = 0; right = 0
            for id2 in current_ids:
                if id == id2: continue
                which_side = planes[id, 0] * point_groups[id2][:, 0] + planes[id, 1] * point_groups[id2][:,1] + planes[id, 2] * point_groups[id2][:, 2] + planes[id, 3]
                which_side = (which_side < 0)
                left+=(which_side).all(axis=-1)
                right+=(~which_side).all(axis=-1)
                # left+= (which_side < 0).all(axis=-1)  ### check for how many planes all points of these planes fall on the left of the current plane
                # right+= (which_side > 0).all(axis=-1)  ### check for how many planes all points of these planes fall on the right of the current plane
            if left == current_ids.shape[0]-1 or right == current_ids.shape[0]-1:
                return i

            left_right.append([left, right])

        left_right = np.array(left_right)
        # left_right = left_right.sum(axis=1)
        left_right = np.product(left_right,axis=1)
        best_plane_id = np.argmax(left_right)

        return best_plane_id

    def _split_planes(self,best_plane_id,current_ids,planes,plane_split_count,halfspaces,point_groups, th=1):

        '''
        :param best_plane_id:
        :param current_ids:
        :param planes:
        :param point_groups: padded 2d array of point groups with NaNs
        :param n_points_per_plane: real number of points per group (ie plane)
        :return: left and right planes
        '''

        best_plane = planes[current_ids[best_plane_id]]

        ### now put the planes into the left and right subspace of the best_plane split
        ### planes that lie in both subspaces are split (ie their point_groups are split) and appended as new planes to the planes array, and added to both subspaces
        left_planes = []
        right_planes = []
        for id in current_ids:

            if id == current_ids[best_plane_id]:
                continue

            which_side = best_plane[0] * point_groups[id][:, 0] + best_plane[1] * point_groups[id][:, 1] + best_plane[2] * point_groups[id][:, 2] + best_plane[3]

            left_points = point_groups[id][which_side < 0, :]
            right_points = point_groups[id][which_side > 0, :]

            assert (point_groups[id].shape[0] > th)  # threshold cannot be bigger than the detection threshold

            if (point_groups[id].shape[0] - left_points.shape[0]) < th:
                left_planes.append(id)
                point_groups[id] = left_points  # update the point group, in case some points got dropped according to threshold
            elif(point_groups[id].shape[0] - right_points.shape[0]) < th:
                right_planes.append(id)
                point_groups[id] = right_points # update the point group, in case some points got dropped according to threshold
            else:
                # print("id:{}: total-left/right: {}-{}/{}".format(current_ids[best_plane_id],n_points_per_plane[id],left_points.shape[0],right_points.shape[0]))
                if (left_points.shape[0] > th):
                    left_planes.append(planes.shape[0])
                    point_groups.append(left_points)
                    planes = np.vstack((planes, planes[id]))
                    halfspaces = np.vstack((halfspaces,halfspaces[id]))
                    plane_split_count.append(plane_split_count[id]+1)
                if (right_points.shape[0] > th):
                    right_planes.append(planes.shape[0])
                    point_groups.append(right_points)
                    planes = np.vstack((planes, planes[id]))
                    halfspaces = np.vstack((halfspaces,halfspaces[id]))
                    plane_split_count.append(plane_split_count[id]+1)

                self.split_count+=1

                # planes[id, :] = np.nan
                # point_groups[id][:, :] = np.nan

        return left_planes,right_planes, planes, plane_split_count, halfspaces, point_groups


    def _which_side(self,points,plane):

        points = np.array(points,dtype=float)

        which_side = plane[0] * points[:, 0] + plane[1] * points[:, 1] + plane[2] * points[:, 2] + plane[3]
        left = which_side <= 0
        right = which_side >=0

        return left,right

    def _init_polygons(self):

        """
        1. intersects all pairs of polyhedra that share an edge in the graph and store the intersections on the edge
        2. init an empty vertices list needed for self.construct_polygons
        """

        for e0,e1 in self.graph.edges:

            edge = self.graph.edges[e0,e1]
            c0 = self.graph.nodes[e0]["convex"]
            c1 = self.graph.nodes[e1]["convex"]
            edge["intersection"] = c0.intersection(c1)
            edge["vertices"] =  []

        self.polygons_initialized = True

    def simplify(self):

        def filter_edge(n0,n1):
            # return not self.graph.edges[n0,n1]["processed"]
            to_process = ((self.graph.nodes[n0]["occupancy"] == self.graph.nodes[n1]["occupancy"]) and self.graph.edges[n0,n1]["convex_intersection"])
            return to_process

        k=0
        count = 0
        processed = list(nx.get_edge_attributes(self.graph,"processed").values())
        print(len(self.graph.edges))
        edges = list(nx.subgraph_view(self.graph,filter_edge=filter_edge).edges)
        deleted_nodes=[]
        while len(edges):

            for c0,c1 in edges:

                if c0 in deleted_nodes or c1 in deleted_nodes: continue

                nx.contracted_edge(self.graph, (c0, c1), self_loops=False, copy=False)
                # self.graph.nodes[c0]["convex"] = self.graph.nodes[c0]["convex"].convex_hull(self.graph.nodes[c0]["contraction"][c1]["convex"])
                parent = self.tree.parent(c0)
                pp_id = self.tree.parent(parent.identifier).identifier
                self.graph.nodes[c0]["convex"] = parent.data["convex"]

                self.tree.remove_node(parent.identifier)
                deleted_nodes.append(c1)

                dd = {"convex": parent.data["convex"], "plane_ids": parent.data["plane_ids"]}
                self.tree.create_node(tag=c0, identifier=c0, data=dd, parent=pp_id)

                sibling =  self.tree.siblings(c0)[0]
                if sibling.is_leaf():
                    self.graph.edges[c0, sibling.identifier]["convex_intersection"] = True
                    self.graph.edges[c0, sibling.identifier]["processed"] = False

            edges = list(nx.subgraph_view(self.graph, filter_edge=filter_edge).edges)

        self.cells = list(nx.get_node_attributes(self.graph, "convex").values())
        self._init_polygons()


    # def simplify(self):
    #
    #     k=0
    #     count = 0
    #     processed = list(nx.get_edge_attributes(self.graph,"processed").values())
    #     print(len(self.graph.edges))
    #     while not np.array(processed).all():
    #         count+=1
    #         c0,c1 = list(self.graph.edges)[k]
    #
    #         if self.graph[c0][c1]["processed"]:
    #             k = (k + 1) % len(list(self.graph.edges))
    #             continue
    #
    #         if (self.graph.nodes[c0]["occupancy"] == 1 and self.graph.nodes[c1]["occupancy"] == 1) and self.graph[c0][c1]["convex_intersection"]:
    #             nx.contracted_edge(self.graph, (c0, c1), self_loops=False, copy=False)
    #             # self.graph.nodes[c0]["convex"] = self.graph.nodes[c0]["convex"].convex_hull(self.graph.nodes[c0]["contraction"][c1]["convex"])
    #             parent = self.tree.parent(c0)
    #             pp_id = self.tree.parent(parent.identifier).identifier
    #             self.graph.nodes[c0]["convex"] = parent.data["convex"]
    #
    #             #self.tree.remove_subtree(parent.identifier)
    #             self.tree.remove_node(parent.identifier)
    #
    #             dd = {"convex": parent.data["convex"], "plane_ids": parent.data["plane_ids"]}
    #             self.tree.create_node(tag=c0, identifier=c0, data=dd, parent=pp_id)
    #
    #             sibling =  self.tree.siblings(c0)[0]
    #             if sibling.is_leaf():
    #                 self.graph.edges[c0, sibling.identifier]["convex_intersection"] = True
    #                 self.graph.edges[c0, sibling.identifier]["processed"] = False
    #
    #         else:
    #             self.graph[c0][c1]["processed"] = True
    #
    #         k = (k+1)%len(list(self.graph.edges))
    #         processed = list(nx.get_edge_attributes(self.graph,"processed").values())
    #         a=5
    #
    #     print(count)
    #     a=5
    #
    #     self.cells = list(nx.get_node_attributes(self.graph, "convex").values())
    #     self._init_polygons()
    #




    def collapse_convex_intersections(self):

        """same as simplify but only one iteration, ie no processing of updated edges"""

        deleted_nodes = []
        for c0, c1 in list(self.graph.edges):
            if c0 in deleted_nodes or c1 in deleted_nodes: continue
            if self.graph.nodes[c0]["occupancy"] == self.graph.nodes[c1]["occupancy"]:

                if self.graph[c0][c1]["convex_intersection"]:

                    nx.contracted_edge(self.graph, (c0, c1), self_loops=False, copy=False)
                    self.graph.nodes[c0]["convex"] = self.graph.nodes[c0]["convex"].convex_hull(self.graph.nodes[c0]["contraction"][c1]["convex"])
                    # self.graph.nodes[c0]["convex"] = self.tree.parent(c0).data["convex"]
                    deleted_nodes.append(c1)

        self.cells = list(nx.get_node_attributes(self.graph, "convex").values())
        self._init_polygons()

    def construct_polygons(self):

        """adds missing vertices to the polyhedron facets by intersecting all neighbors with all neighbors"""

        if not self.polygons_initialized:
            self._init_polygons()

        for c0,c1 in list(self.graph.edges):

            if self.graph.nodes[c0]["occupancy"] != self.graph.nodes[c1]["occupancy"]:
                current_edge = self.graph[c0][c1]
                current_facet = current_edge["intersection"]

                for neighbor in list(self.graph[c0]):
                    if neighbor == c1: continue

                    this_edge = self.graph[c0][neighbor]
                    this_facet = this_edge["intersection"]
                    facet_intersection = current_facet.intersection(this_facet)

                    if facet_intersection.dim() == 0 or facet_intersection.dim() == 1:
                        current_edge["vertices"]+=facet_intersection.vertices_list()
                        this_edge["vertices"]+=facet_intersection.vertices_list()

                for neighbor in list(self.graph[c1]):
                    if neighbor == c0: continue

                    this_edge = self.graph[c1][neighbor]
                    this_facet = this_edge["intersection"]
                    facet_intersection = current_facet.intersection(this_facet)

                    if facet_intersection.dim() == 0 or facet_intersection.dim() == 1:
                        current_edge["vertices"] += facet_intersection.vertices_list()
                        this_edge["vertices"] += facet_intersection.vertices_list()



    def construct_partition(self, m, mode=Tree.DEPTH, th=1, ordering="optimal", export=False):


        ## Tree.DEPTH seems slightly faster then Tree.WIDTH

        # TODO: i need a secomd ordering for when two planes have the same surface split score, take the one with the bigger area.
        # because randommly shuffling the planes before this function has a big influence on the result

        # The tag property of tree.node is what is shown when you call tree.show(). can be changed with tree.node(NODEid).tag = "some_text"

        ### make a new planes array, to which planes that are split can be appanded
        planes = deepcopy(self.planes)
        halfspaces = deepcopy(self.halfspaces)
        point_groups = list(self.points)
        plane_split_count =[0]*len(self.planes)
        plane_colors = [[0,1,0],[1,0,0],[0,0,1],[139/255,0,139/255]]

        cell_count = 0
        self.split_count = 0


        ## init the graph
        graph = nx.Graph()
        graph.add_node(cell_count, convex=self.bounding_poly)

        ## expand the tree as long as there is at least one plane in any of the subspaces
        tree = Tree()
        dd = {"convex": self.bounding_poly, "plane_ids": np.arange(self.planes.shape[0])}
        tree.create_node(tag=cell_count, identifier=cell_count, data=dd)  # root node
        children = tree.expand_tree(0, filter=lambda x: x.data["plane_ids"].shape[0], mode=mode)
        plane_count = 0
        edge_id=0
        convex_intersection=False
        for child in children:

            current_ids = tree[child].data["plane_ids"]

            ### get the best plane
            if ordering == "optimal":
                best_plane_id = self._get_best_plane(current_ids,planes,point_groups)
            else:
                best_plane_id = 0
            best_plane = planes[current_ids[best_plane_id]]
            plane_count+=1

            ### split the planes
            left_planes, right_planes, planes, plane_split_count, halfspaces, point_groups, = self._split_planes(best_plane_id,current_ids,planes,plane_split_count,halfspaces,point_groups, th)

            ### export best plane
            if export:
                epoints = point_groups[current_ids[best_plane_id]]
                epoints = epoints[~np.isnan(epoints).all(axis=-1)]
                nsplits = plane_split_count[current_ids[best_plane_id]]
                color = plane_colors[nsplits] if nsplits < 4 else plane_colors[3]
                if nsplits > 0:
                    a=5
                if epoints.shape[0]>3:
                    self.planeExporter.export_plane(os.path.dirname(m["planes"]), best_plane, epoints,count=str(plane_count),color=color)


            ## create the new convexes
            current_cell = tree[child].data["convex"]
            hspace_positive, hspace_negative = halfspaces[current_ids[best_plane_id],0], halfspaces[current_ids[best_plane_id],1]

            cell_negative = current_cell.intersection(hspace_negative)
            cell_positive = current_cell.intersection(hspace_positive)

            ## update tree by creating the new nodes with the planes that fall into it
            ## and update graph with new nodes
            if(cell_negative.dim() == 3):
            # if(not cell_negative.is_empty()):
                if export:
                    self.cellComplexExporter.write_cells(m,cell_negative,count=str(cell_count+1)+"n")
                dd = {"convex": cell_negative,"plane_ids": np.array(left_planes)}
                cell_count = cell_count+1
                neg_cell_count = cell_count
                tree.create_node(tag=cell_count, identifier=cell_count, data=dd, parent=tree[child].identifier)
                graph.add_node(neg_cell_count,convex=cell_negative)

            if(cell_positive.dim() == 3):
            # if(not cell_positive.is_empty()):
                if export:
                    self.cellComplexExporter.write_cells(m,cell_positive,count=str(cell_count+1)+"p")
                dd = {"convex": cell_positive,"plane_ids": np.array(right_planes)}
                cell_count = cell_count+1
                pos_cell_count = cell_count
                tree.create_node(tag=cell_count, identifier=cell_count, data=dd, parent=tree[child].identifier)
                graph.add_node(pos_cell_count,convex=cell_positive)

            # if(not cell_positive.is_empty() and not cell_negative.is_empty()):
            if(cell_positive.dim() == 3 and cell_negative.dim() == 3):
                new_intersection = cell_negative.intersection(cell_positive)
                graph.add_edge(cell_count-1, cell_count, intersection=new_intersection, vertices=[],
                               supporting_plane=best_plane,id=edge_id,color=(np.random.rand(3)*255).astype(int),
                               convex_intersection=True, processed=False)
                if export:
                    self.cellComplexExporter.write_facet(m,new_intersection,count=plane_count)

            ## add edges to other cells, these must be neigbors of the parent (her named child) of the new subspaces
            neighbors_of_old_cell = list(graph[child])
            old_cell_id=child
            for neighbor_id_old_cell in neighbors_of_old_cell:
                # get the neighboring convex
                nconvex = graph.nodes[neighbor_id_old_cell]["convex"]
                # intersect new cells with old neighbors to make the new facets
                negative_intersection = nconvex.intersection(cell_negative)
                positive_intersection = nconvex.intersection(cell_positive)

                n_nonempty = negative_intersection.dim()==2
                p_nonempty = positive_intersection.dim()==2
                # add the new edges (from new cells with intersection of old neighbors) and move over the old additional vertices to the new
                if n_nonempty:
                    # convex_intersection = (graph[old_cell_id][neighbor_id_old_cell]["convex_intersection"] \
                    #     and (graph[old_cell_id][neighbor_id_old_cell]["intersection"] == negative_intersection))
                    graph.add_edge(neighbor_id_old_cell,neg_cell_count,intersection=negative_intersection, vertices=[],
                                   supporting_plane=graph[neighbor_id_old_cell][old_cell_id]["supporting_plane"],
                                   id=edge_id,color=(np.random.rand(3)*255).astype(int),convex_intersection=convex_intersection, processed=False)
                if p_nonempty:
                    # convex_intersection = (graph[old_cell_id][neighbor_id_old_cell]["convex_intersection"] \
                    #     and (graph[old_cell_id][neighbor_id_old_cell]["intersection"] == positive_intersection))
                    graph.add_edge(neighbor_id_old_cell, pos_cell_count, intersection=positive_intersection, vertices=[],
                                   supporting_plane=graph[neighbor_id_old_cell][old_cell_id]["supporting_plane"],
                                   id=edge_id,color=(np.random.rand(3)*255).astype(int),convex_intersection=convex_intersection, processed=False)


            # nx.draw(graph,with_labels=True)  # networkx draw()
            # plt.draw()
            # plt.show()
            # #
            # self.cellComplexExporter.write_graph(m,graph)
            # self.cells = list(nx.get_node_attributes(graph, "convex").values())
            # self.save_obj(os.path.join(m["abspy"]["partition"]))

            ## remove the parent node
            graph.remove_node(child)


        self.graph = graph
        self.tree = tree
        if export:
            self.cellComplexExporter.write_graph(m,graph)

        self.cells = list(nx.get_node_attributes(graph, "convex").values())

        self.constructed = True
        self.polygons_initialized = True

        # self.regularize_polygon_edges()



        logger.info("Out of {} planes {} were split, making a total of {} planes now".format(len(self.planes),self.split_count,len(self.planes)+self.split_count))

        return 0


    def prioritise_planes(self, mode = ["vertical", "random", "norm"]):
        """
        Prioritise certain planes to favour building reconstruction.

        First, vertical planar primitives are accorded higher priority than horizontal or oblique ones
        to avoid incomplete partitioning due to missing data about building facades.
        Second, in the same priority class, planar primitives with larger areas are assigned higher priority
        than smaller ones, to make the final cell complex as compact as possible.
        Note that this priority setting is designed exclusively for building models.

        Parameters
        ----------
        prioritise_verticals: bool
            Prioritise vertical planes if set True
        """
        logger.info('prioritising planar primitives')



        indices_sorted_planes = np.arange(len(self.planes))

        if mode == "random":
            np.random.shuffle(indices_sorted_planes)
            indices_priority = indices_sorted_planes
        elif mode == "vertical":
            indices_vertical_planes = self._vertical_planes(slope_threshold=0.9)
            bool_vertical_planes = np.in1d(indices_sorted_planes, indices_vertical_planes)
            indices_priority = np.append(indices_sorted_planes[bool_vertical_planes],
                                         indices_sorted_planes[np.invert(bool_vertical_planes)])
        else:
            # compute the priority
            indices_sorted_planes = self._sort_planes(mode)

        # reorder both the planes and their bounds
        self.planes = self.planes[indices_priority]
        self.bounds = self.bounds[indices_priority]
        self.points = self.points[indices_priority]

        # append additional planes with highest priority
        if self.additional_planes is not None:
            self.planes = np.concatenate([self.additional_planes, self.planes], axis=0)
            additional_bounds = [[[-np.inf, -np.inf, -np.inf], [np.inf, np.inf, np.inf]]] * len(self.additional_planes)
            self.bounds = np.concatenate([additional_bounds, self.bounds], axis=0)  # never miss an intersection

        logger.debug('ordered planes: {}'.format(self.planes))
        logger.debug('ordered bounds: {}'.format(self.bounds))

    def _vertical_planes(self, slope_threshold=0.9, epsilon=10e-5):
        """
        Return vertical planes.

        Parameters
        ----------
        slope_threshold: float
            Slope threshold, above which the planes are considered vertical
        epsilon: float
            Trivial term to avoid NaN

        Returns
        -------
        as_int: (n,) int
            Indices of the vertical planar primitives
        """
        slope_squared = (self.planes[:, 0] ** 2 + self.planes[:, 1] ** 2) / (self.planes[:, 2] ** 2 + epsilon)
        return np.where(slope_squared > slope_threshold ** 2)[0]

    def _sort_planes(self, mode='norm'):
        """
        Sort planes.

        Parameters
        ----------
        mode: str
            Mode for sorting, can be 'volume' or 'norm'

        Returns
        -------
        as_int: (n,) int
            Indices by which the planar primitives are sorted based on their bounding box volume
        """
        if mode == 'volume':
            volume = np.prod(self.bounds[:, 1, :] - self.bounds[:, 0, :], axis=1)
            return np.argsort(volume)[::-1]
        elif mode == 'norm':
            sizes = np.linalg.norm(self.bounds[:, 1, :] - self.bounds[:, 0, :], ord=2, axis=1)
            return np.argsort(sizes)[::-1]
        elif mode == 'area':
            # project the points supporting each plane onto the plane
            # https://stackoverflow.com/questions/9605556/how-to-project-a-point-onto-a-plane-in-3d
            raise NotImplementedError
        else:
            raise ValueError('mode has to be "volume" or "norm"')

    @staticmethod
    def _pad_bound(bound, padding=0.00):
        """
        Pad bound.

        Parameters
        ----------
        bound: (2, 3) float
            Bound of the query planar primitive
        padding: float
            Padding factor, defaults to 0.05.

        Returns
        -------
        as_float: (2, 3) float
            Padded bound
        """

        extent = bound[1] - bound[0]
        return [bound[0] - extent * padding, bound[1] + extent * padding]


    def _intersect_bound_plane(self, bound, plane, exhaustive=False, epsilon=10e-5):
        """
        Pre-intersection test between query primitive and existing cells,
        based on AABB and plane parameters.

        Parameters
        ----------
        bound: (2, 3) float
            Bound of the query planar primitive
        plane: (4,) float
            Plane parameters
        exhaustive: bool
            Exhaustive partitioning, only for benchmarking
        epsilon: float
            Distance tolerance

        Returns
        -------
        as_int: (n,) int
            Indices of existing cells whose bounds intersect with bounds of the query primitive
            and intersect with the supporting plane of the primitive
        """
        if exhaustive:
            return np.arange(len(self.cells_bounds))

        # each planar primitive partitions only the 3D cells that intersect with it
        cells_bounds = np.array(self.cells_bounds)  # easier array manipulation
        center_targets = np.mean(cells_bounds, axis=1)  # N * 3
        extent_targets = cells_bounds[:, 1, :] - cells_bounds[:, 0, :]  # N * 3

        if bound[0][0] == -np.inf:
            intersection_bound = np.arange(len(self.cells_bounds))

        else:
            # intersection with existing cells' AABB
            center_query = np.mean(bound, axis=0)  # 3,
            center_distance = np.abs(center_query - center_targets)  # N * 3
            extent_query = bound[1] - bound[0]  # 3,

            # abs(center_distance) * 2 < (query extent + target extent) for every dimension -> intersection
            intersection_bound = np.where(np.all(center_distance * 2 < extent_query + extent_targets + epsilon, axis=1))[0]

        # plane-AABB intersection test from extracted intersection_bound only
        # https://gdbooks.gitbooks.io/3dcollisions/content/Chapter2/static_aabb_plane.html
        # compute the projection interval radius of AABB onto L(t) = center + t * normal
        radius = np.dot(extent_targets[intersection_bound] / 2, np.abs(plane[:3]))
        # compute distance of box center from plane
        distance = np.dot(center_targets[intersection_bound], plane[:3]) + plane[3]
        # intersection between plane and AABB occurs when distance falls within [-radius, +radius] interval
        intersection_plane = np.where(np.abs(distance) <= radius + epsilon)[0]

        return intersection_bound[intersection_plane]

    @staticmethod
    def _inequalities(plane):
        """
        Inequalities from plane parameters.

        Parameters
        ----------
        plane: (4,) float
            Plane parameters

        Returns
        -------
        positive: (4,) float
            Inequality of the positive half-plane
        negative: (4,) float
            Inequality of the negative half-plane
        """
        positive = [QQ(plane[-1]), QQ(plane[0]), QQ(plane[1]), QQ(plane[2])]
        negative = [QQ(-element) for element in positive]
        return positive, negative

    def _index_node_to_cell(self, query):
        """
        Convert index in the node list to that in the cell list.
        The rationale behind is #nodes == #cells (when a primitive is settled down).

        Parameters
        ----------
        query: int
            Query index in the node list

        Returns
        -------
        as_int: int
            Query index in the cell list
        """
        return list(self.graph.nodes).index(query)

    def _intersect_neighbour(self, kwargs):
        """
        Intersection test between partitioned cells and neighbouring cell.
        Implemented for multi-processing across all neighbours.

        Parameters
        ----------
        kwargs: (int, Polyhedron object, Polyhedron object, Polyhedron object)
            (neighbour index, positive cell, negative cell, neighbouring cell)
        """
        n, cell_positive, cell_negative, cell_neighbour = kwargs['n'], kwargs['positive'], kwargs['negative'], kwargs['neighbour']

        interface_positive = cell_positive.intersection(cell_neighbour)

        if interface_positive.dim() == 2:
            # this neighbour can connect with either or both children
            self.graph.add_edge(self.index_node + 1, n, supporting_plane=kwargs["supporting_plane"])
            interface_negative = cell_negative.intersection(cell_neighbour)
            if interface_negative.dim() == 2:
                self.graph.add_edge(self.index_node + 2, n, supporting_plane=kwargs["supporting_plane"])
        else:
            # this neighbour must otherwise connect with the other child
            self.graph.add_edge(self.index_node + 2, n, supporting_plane=kwargs["supporting_plane"])

    def construct_abspy(self, exhaustive=False, num_workers=0):
        """
        Construct cell complex.

        Two-stage primitive-in-cell predicate.
        (1) bounding boxes of primitive and existing cells are evaluated
        for possible intersection. (2), a strict intersection test is performed.

        Generated cells are stored in self.cells.
        * query the bounding box intersection.
        * optional: intersection test for polygon and edge in each potential cell.
        * partition the potential cell into two. rewind if partition fails.

        Parameters
        ----------
        exhaustive: bool
            Do exhaustive partitioning if set True
        num_workers: int
            Number of workers for multi-processing, disabled if set 0
        """

        self.cells_bounds = [self.bounding_poly.bounding_box()]
        self.cells = [self.bounding_poly]

        if exhaustive:
            logger.info('construct exhaustive cell complex'.format())
        else:
            logger.info('construct cell complex'.format())

        tik = time.time()

        pool = None
        if num_workers > 0:
            pool = multiprocessing.Pool(processes=num_workers)

        pbar = range(len(self.bounds)) if self.quiet else trange(len(self.bounds))
        for i in pbar:  # kinetic for each primitive
            # bounding box intersection test
            # indices of existing cells with potential intersections
            indices_cells = self._intersect_bound_plane(self.bounds[i], self.planes[i], exhaustive)
            assert len(indices_cells), 'intersection failed! check the initial bound'

            # half-spaces defined by inequalities
            # no change_ring() here (instead, QQ() in _inequalities) speeds up 10x
            # init before the loop could possibly speed up a bit
            hspace_positive, hspace_negative = [Polyhedron(ieqs=[inequality]) for inequality in
                                                self._inequalities(self.planes[i])]


            # partition the intersected cells and their bounds while doing mesh slice plane
            indices_parents = []

            for index_cell in indices_cells:
                cell_positive = hspace_positive.intersection(self.cells[index_cell])
                cell_negative = hspace_negative.intersection(self.cells[index_cell])

                if cell_positive.dim() != 3 or cell_negative.dim() != 3:
                    # if cell_positive.is_empty() or cell_negative.is_empty():
                    """
                    cannot use is_empty() predicate for degenerate cases:
                        sage: Polyhedron(vertices=[[0, 1, 2]])
                        A 0-dimensional polyhedron in ZZ^3 defined as the convex hull of 1 vertex
                        sage: Polyhedron(vertices=[[0, 1, 2]]).is_empty()
                        False
                    """
                    continue

                # incrementally build the adjacency graph
                if self.graph is not None:
                    # append the two nodes (UID) being partitioned
                    self.graph.add_node(self.index_node + 1,convex=cell_positive)
                    self.graph.add_node(self.index_node + 2,convex=cell_negative)

                    # append the edge in between
                    self.graph.add_edge(self.index_node + 1, self.index_node + 2,supporting_plane=self.planes[i])

                    # get neighbours of the current cell from the graph
                    neighbours = self.graph[list(self.graph.nodes)[index_cell]]  # index in the node list

                    if neighbours:
                        # get the neighbouring cells to the parent
                        cells_neighbours = [self.cells[self._index_node_to_cell(n)] for n in neighbours]

                        # adjacency test between both created cells and their neighbours
                        # todo:
                        #   Avoid 3d-3d intersection if possible. Unsliced neighbours connect with only one child;
                        #   sliced neighbors connect with both children.

                        kwargs = []
                        for n, cell in zip(neighbours, cells_neighbours):
                            supporting_plane = self.graph.edges[list(self.graph.nodes)[index_cell],n]["supporting_plane"]
                            kwargs.append({'n': n, 'positive': cell_positive, 'negative': cell_negative, 'neighbour': cell,
                                           'supporting_plane':supporting_plane})

                        if pool is None:
                            for k in kwargs:
                                self._intersect_neighbour(k)
                        else:
                            pool.map(self._intersect_neighbour, kwargs)

                    # update cell id
                    self.index_node += 2

                self.cells.append(cell_positive)
                self.cells.append(cell_negative)

                # incrementally cache the bounds for created cells
                self.cells_bounds.append(cell_positive.bounding_box())
                self.cells_bounds.append(cell_negative.bounding_box())

                indices_parents.append(index_cell)

            # delete the parent cells and their bounds. this does not affect the appended ones
            for index_parent in sorted(indices_parents, reverse=True):
                del self.cells[index_parent]
                del self.cells_bounds[index_parent]

                # remove the parent node (and subsequently its incident edges) in the graph
                if self.graph is not None:
                    self.graph.remove_node(list(self.graph.nodes)[index_parent])

        self.constructed = True
        logger.debug('cell complex constructed: {:.2f} s'.format(time.time() - tik))


    @property
    def num_cells(self):
        """
        Number of cells in the complex.
        """
        return len(self.cells)

    @property
    def num_planes(self):
        """
        Number of planes in the complex, excluding the initial bounding box.
        """
        return len(self.planes)

    def volumes(self, multiplier=1.0, engine='Qhull'):
        """
        list of cell volumes.

        Parameters
        ----------
        multiplier: float
            Multiplier to the volume
        engine: str
            Engine to compute volumes, can be 'Qhull' or 'Sage' with native SageMath

        Returns
        -------
        as_float: list of float
            Volumes of cells
        """
        if engine == 'Qhull':
            from scipy.spatial import ConvexHull
            volumes = [None for _ in range(len(self.cells))]
            for i, cell in enumerate(self.cells):
                try:
                    volumes[i] = ConvexHull(cell.vertices_list()).volume * multiplier
                except:
                    # degenerate floating-point
                    volumes[i] = RR(cell.volume()) * multiplier
            return volumes

        elif engine == 'Sage':
            return [RR(cell.volume()) * multiplier for cell in self.cells]

        else:
            raise ValueError('engine must be either "Qhull" or "Sage"')


    def print_info(self):
        """
        Print info to console.
        """
        logger.info('number of planes: {}'.format(self.num_planes))
        logger.info('number of cells: {}'.format(self.num_cells))





