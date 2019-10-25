from phi.physics.physics import State
from phi import math, struct
from .flag import Flag, _PROPAGATOR
from phi.geom import Geometry
import numpy as np


def _to_valid_data(data):
    if data is None: return None
    if isinstance(data, (tuple, list)):
        return np.array(data)  # numbers or objects
    else:
        return data


class Field(State):
    __struct__ = State.__struct__.extend(['_data'], ['_bounds', '_name', '_flags'])

    def __init__(self, name, bounds, data, flags=(), batch_size=None):
        State.__init__(self, tags=[name, 'field'], batch_size=batch_size)
        assert bounds is None or isinstance(bounds, Geometry), 'bounds must be of type Geometry but got "%s"' % bounds
        self._data = _to_valid_data(data)
        self._name = name
        self._bounds = bounds
        self._flags = flags

    def __validate_flags__(self):
        if self._flags is None:
            self._flags = None
        else:
            self._flags = tuple(set(self._flags))  # remove duplicates
            for flag in self._flags:
                if not flag.is_applicable(self.rank, self.component_count):
                    raise ValueError('Flag "%s" is not applicable to field %s' % (flag, self))

    def __validate_data__(self):
        self._data = _to_valid_data(self._data)

    def with_data(self, data):
        return self.copied_with(data=data, flags=())

    @property
    def dtype(self):
        return math.dtype(self._data)

    @property
    def name(self):
        return self._name

    @property
    def data(self):
        """
Data holds the values of this field according to the order specified by points.
For composite fields, data holds a tuple of component fields.
        :return: n-dimensional tensor
        """
        return self._data

    @property
    def bounds(self):
        """
The bounds describe the spatial region inside which this field is defined.
Outside of bounds, the field is assumed to be zero / undefined.
Fields with infinite range (such as extrapolated fields ) have bounds None.
        :return:
        """
        return self._bounds

    @property
    def flags(self):
        """
Flags describe properties of a Field such as divergence-freeness.
        :return: tuple of flags
        """
        return self._flags

    def sample_at(self, points, collapse_dimensions=True):
        """
Resample this field at the given points.
The value of points that lie outside the bounds of this Field is undefined.
        :param points: tensor or rank >= 2 containing world-space vectors
        :param collapse_dimensions: if True, collapses dimensions to 1 along which all values would be equal.
        :return: tensor of shape location.shape[:-1]+[components]
        """
        raise NotImplementedError(self)

    def at(self, other_field, collapse_dimensions=True, force_optimization=False):
        """
Resample this field at the same points as other_field.
The returned Field is compatible with other_field.
The value of points that lie outside the bounds of this Field is undefined.
        :param location: Field
        :param collapse_dimensions: if True, collapses dimensions to 1 along which all values would be equal.
        :param force_optimization: If true, this algorithm either uses an optimized implementation
        :return: a new Field which samples all components of this field at the points of other_field
        """
        if force_optimization:
            raise ValueError('No optimized resample algorithm found for fields %s, %s' % (self, other_field))
        try:
            resampled = self.sample_at(other_field.points.data, collapse_dimensions=collapse_dimensions)
            return other_field.copied_with(data=resampled, flags=propagate_flags_resample(self, other_field.flags, other_field.rank))
        except StaggeredSamplePoints:  # other_field is staggered
            return broadcast_at(self, other_field)

    @property
    def rank(self):
        """
        Spatial rank of the field (1 for 1D, 2 for 2D, 3 for 3D).
        Note that this does not indicate the shape of the data array.
        If the field is independent of the dimensionality, the rank property is None.
        :return: int
        """
        raise NotImplementedError(self)

    @property
    def component_count(self):
        """
Number of components of this Field.
The components can be sampled at the same points or at different points (like with StaggeredGrids).
        :return: int
        """
        raise NotImplementedError(self)

    def unstack(self):
        """
Split the Field by components.
If the field only has one component, returns a list containing itself.
        :return: tuple of Fields
        """
        raise NotImplementedError(self)

    @property
    def points(self):
        """
Returns a Field containing all sample points of this field.
The returned Field is compatible with this one.
If the components of this field are sampled at different locations, this method raises StaggeredSamplePoints.
If this field has no sample points, points is None.
        :return: vector Field
        """
        raise NotImplementedError(self)

    @property
    def has_points(self):
        try:
            return self.points is not None
        except StaggeredSamplePoints:
            return True

    def compatible(self, other_field):
        """
Checks if two Fields have the same sample points and values are stored in the same order.
For performance reasons, this method does not actually check every single point.
Even if this method returns False, the sample points may still be the same.
        :param other_field:
        :return: True if both Fields have the same sample points.
        """
        raise NotImplementedError(self)

    def __mul__(self, other):
        return self.__dataop__(other, True, lambda d1, d2: d1 * d2)

    __rmul__ = __mul__

    def __sub__(self, other):
        return self.__dataop__(other, False, lambda d1, d2: d1 - d2)

    def __rsub__(self, other):
        return self.__dataop__(other, False, lambda d1, d2: d2 - d1)

    def __add__(self, other):
        return self.__dataop__(other, False, lambda d1, d2: d1 + d2)

    __radd__ = __add__

    def __dataop__(self, other, linear_if_scalar, data_operator):
        if isinstance(other, Field):
            assert self.compatible(other), 'Fields are not compatible: %s and %s' % (self, other)
            flags = propagate_flags_operation(self.flags+other.flags, False, self.rank, self.component_count)
            self_data = self.data if self.has_points else self.at(other).data
            other_data = other.data if other.has_points else other.at(self).data
            data = data_operator(self_data, other_data)
        else:
            flags = propagate_flags_operation(self.flags, linear_if_scalar, self.rank, self.component_count)
            data = data_operator(self.data, other)
        return self.copied_with(data=data, flags=flags)



class StaggeredSamplePoints(Exception):
    def __init__(self, *args):
        Exception.__init__(self, *args)


class IncompatibleFieldTypes(Exception):
    def __init__(self, *args):
        Exception.__init__(self, *args)


def propagate_flags_resample(data_field, structure_flags, resulting_rank):
    flags = []
    for flag in data_field.flags:
        if flag.is_data_bound and \
                flag.propagates(_PROPAGATOR.RESAMPLE) and \
                flag.is_applicable(resulting_rank, data_field.component_count):
            flags.append(flag)
    for flag in structure_flags:
        if flag.is_structure_bound and \
                flag.propagates(_PROPAGATOR.RESAMPLE) and \
                flag.is_applicable(resulting_rank, data_field.component_count):
            flags.append(flag)
    return tuple(flags)


def propagate_flags_children(flags, child_rank, child_component_count):
    result = []
    for flag in flags:
        if flag.propagates(_PROPAGATOR.CHILDREN) and flag.is_applicable(child_rank, child_component_count):
            result.append(flag)
    return tuple(result)


def propagate_flags_operation(flags, is_linear, result_rank, result_components):
    result = []
    propagator = _PROPAGATOR.LINEAR_OPERATIONS if is_linear else _PROPAGATOR.ALL_OPERATIONS
    for flag in flags:
        if flag.is_data_bound and\
                flag.propagates(propagator) and\
                flag.is_applicable(result_rank, result_components):
            result.append(flag)
    return tuple(result)


def broadcast_at(field1, field2):
    if field1.component_count != field2.component_count and field1.component_count != 1:
        raise IncompatibleFieldTypes('Can only resample to staggered fields with same number of components.\n%s\n%s' % (field1, field2))
    if field1.component_count == 1:
        new_components = [field1.at(f2) for f2 in field2.unstack()]
    else:
        new_components = [f1.at(f2) for f1, f2 in zip(field1.unstack(), field2.unstack())]
    return field2.copied_with(data=tuple(new_components), flags=propagate_flags_resample(field1, field2.flags, field2.rank))