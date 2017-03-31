from __future__ import absolute_import, division, print_function

import pkg_resources
import theano
import warnings

from theano import Op

from theano.gpuarray import basic_ops, GpuArrayType

from pygpu.gpuarray import GpuKernel, GpuArray
from string import Template

import numpy as np
from numpy.linalg.linalg import LinAlgError

try:
    import pygpu
    from pygpu.basic import triu, tril
    pygpu_available = True
except ImportError:
    pygpu_available = False

cusolver_available = False
try:
    import skcuda
    from skcuda import cusolver
    cusolver_available = True
except (ImportError, OSError, RuntimeError, pkg_resources.DistributionNotFound):
    pass

if cusolver_available:
    # Add cusolver call as it is missing in skcuda
    # SPOTRS
    cusolver._libcusolver.cusolverDnSpotrs.restype = int
    cusolver._libcusolver.cusolverDnSpotrs.argtypes = [cusolver.ctypes.c_void_p,
                                                       cusolver.ctypes.c_int,
                                                       cusolver.ctypes.c_int,
                                                       cusolver.ctypes.c_int,
                                                       cusolver.ctypes.c_void_p,
                                                       cusolver.ctypes.c_int,
                                                       cusolver.ctypes.c_void_p,
                                                       cusolver.ctypes.c_int,
                                                       cusolver.ctypes.c_void_p]

    def cusolverDnSpotrs(handle, uplo, n, nrhs, A, lda,
                         B, ldb, devInfo):
        """
        Solve real single precision linear system for hermitian matrices.
        References
        ----------
        `cusolverDn<t>potrs <http://docs.nvidia.com/cuda/cusolver/index.html#cuds-lt-t-gt-potrs>`_
        """

        status = cusolver._libcusolver.cusolverDnSpotrs(handle, uplo, n, nrhs,
                                                        int(A), lda, int(B),
                                                        ldb, int(devInfo))
        cusolver.cusolverCheckStatus(status)


def attach_cusolver_handle_to_context(ctx):
    handle = getattr(ctx, 'cusolver_handle', None)
    if handle is None:
        with ctx:
            ctx.cusolver_handle = cusolver.cusolverDnCreate()


class GpuCusolverSolve(Op):
    """
    CUSOLVER GPU solver OP.

    Parameters
    ----------
    trans
        Whether to take the transpose of the input matrix or not.

    """

    __props__ = ('A_structure', 'trans', 'inplace')

    def __init__(self, A_structure='general', trans='N', inplace=False):
        self.trans = trans
        self.inplace = inplace
        self.A_structure = A_structure
        if self.inplace:
            self.destroy_map = {0: [0, 1]}
        super(GpuCusolverSolve, self).__init__()

    def make_node(self, inp1, inp2):
        if not cusolver_available:
            raise RuntimeError('CUSOLVER is not available and '
                               'GpuCusolverSolve Op can not be constructed.')
        if skcuda.__version__ <= '0.5.1':
            warnings.warn('The GpuSolve op requires scikit-cuda > 0.5.1 to work with CUDA 8')
        context_name = basic_ops.infer_context_name(inp1, inp2)

        inp1 = basic_ops.as_gpuarray_variable(inp1, context_name)
        inp2 = basic_ops.as_gpuarray_variable(inp2, context_name)

        inp1 = basic_ops.gpu_contiguous(inp1)
        inp2 = basic_ops.gpu_contiguous(inp2)

        # this op can only operate on float32 matrices
        assert inp1.ndim == 2
        assert inp2.ndim == 2
        assert inp1.dtype == 'float32'
        assert inp2.dtype == 'float32'

        return theano.Apply(
            self, [inp1, inp2],
            [GpuArrayType('float32',
                          broadcastable=inp1.broadcastable,
                          context_name=context_name)()])

    def prepare_node(self, node, storage_map, compute_map, impl):
        ctx = node.inputs[0].type.context
        attach_cusolver_handle_to_context(ctx)

    def check_dev_info(self, dev_info):
        val = np.asarray(dev_info)[0]
        if val > 0:
            raise LinAlgError('A is singular')

    def perform(self, node, inputs, outputs):
        context = inputs[0][0].context

        # Size of the matrices to invert.
        z = outputs[0]

        # Matrix.
        A = inputs[0]

        # Solution vectors.
        b = inputs[1]

        assert(len(A.shape) == 2)
        assert(len(b.shape) == 2)

        if self.trans in ['T', 'C']:
            trans = 1
            l, n = A.shape
            k, m = b.shape
        elif self.trans == 'N':
            trans = 0
            n, l = A.shape
            k, m = b.shape
        else:
            raise ValueError('Invalid value for trans')
        if l != n:
            raise ValueError('A must be a square matrix')
        if n != k:
            raise ValueError('A and b must be aligned.')

        lda = max(1, n)
        ldb = max(1, k)

        # We copy A and b as cusolver operates inplace
        b = pygpu.array(b, copy=True, order='F')
        if not self.inplace:
            A = pygpu.array(A, copy=True)
        A_ptr = A.gpudata
        b_ptr = b.gpudata

        # cusolver expects a F ordered matrix, but A is not explicitly
        # converted between C and F order, instead we switch the
        # "transpose" flag.
        if A.flags['C_CONTIGUOUS']:
            trans = 1 - trans

        if self.A_structure == 'symmetric':
            with context:
                workspace_size = cusolver.cusolverDnSpotrf_bufferSize(
                    context.cusolver_handle, 0, n, A_ptr, lda)

            workspace = pygpu.zeros(workspace_size, dtype='float32',
                                    context=context)

            dev_info = pygpu.zeros((1,), dtype='int32', context=context)

            workspace_ptr = workspace.gpudata
            dev_info_ptr = dev_info.gpudata

            with context:
                cusolver.cusolverDnSpotrf(
                    context.cusolver_handle, 0, n, A_ptr, lda, workspace_ptr,
                    workspace_size, dev_info_ptr)
                self.check_dev_info(dev_info)

                cusolverDnSpotrs(
                    context.cusolver_handle, 0, n, m, A_ptr, lda,
                    b_ptr, ldb, dev_info_ptr)

        else:
            # general case for A
            with context:
                workspace_size = cusolver.cusolverDnSgetrf_bufferSize(
                    context.cusolver_handle, n, n, A_ptr, lda)

            workspace = pygpu.zeros(workspace_size, dtype='float32',
                                    context=context)

            pivots = pygpu.zeros(n, dtype='int32', context=context)

            dev_info = pygpu.zeros((1,), dtype='int32', context=context)

            workspace_ptr = workspace.gpudata
            pivots_ptr = pivots.gpudata
            dev_info_ptr = dev_info.gpudata

            with context:
                cusolver.cusolverDnSgetrf(
                    context.cusolver_handle, n, n, A_ptr, lda, workspace_ptr,
                    pivots_ptr, dev_info_ptr)
                self.check_dev_info(dev_info)

                cusolver.cusolverDnSgetrs(
                    context.cusolver_handle, trans, n, m, A_ptr, lda,
                    pivots_ptr, b_ptr, ldb, dev_info_ptr)

        z[0] = b


def gpu_solve(A, b, A_structure='general', trans='N'):
    return GpuCusolverSolve(A_structure, trans)(A, b)


class GpuCholesky(Op):
    """
    CUSOLVER GPU Cholesky Op.

    Given a real positive definite matrix `A` returns either a lower
    triangular matrix `L` such that `A == dot(L, L.T)` if `lower == True`
    else returns an upper triangular matrix `U` such that `A == dot(U.T, U)`
    if `lower == False`.

    Parameters
    ----------
    lower
        Whether to return a lower rather than upper triangular decomposition.

    """

    __props__ = ('lower', 'inplace')

    def __init__(self, lower=True, inplace=False):
        self.lower = lower
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [0]}
        super(GpuCholesky, self).__init__()

    def make_node(self, inp):
        if not cusolver_available:
            raise RuntimeError('CUSOLVER is not available and '
                               'GpuCholesky Op can not be constructed.')
        if skcuda.__version__ <= '0.5.1':
            warnings.warn('The GpuSolve op requires scikit-cuda > 0.5.1 to work with CUDA 8')
        if not pygpu_available:
            raise RuntimeError('Missing pygpu or triu/tril functions.'
                               'Try updating libgpuarray?')
        context_name = basic_ops.infer_context_name(inp)

        inp = basic_ops.as_gpuarray_variable(inp, context_name)

        inp = basic_ops.gpu_contiguous(inp)

        # this op can only operate on float32 matrices
        assert inp.ndim == 2
        assert inp.dtype == 'float32'

        return theano.Apply(
            self, [inp],
            [GpuArrayType('float32',
                          broadcastable=inp.broadcastable,
                          context_name=context_name)()])

    def prepare_node(self, node, storage_map, compute_map, impl):
        ctx = node.inputs[0].type.context
        attach_cusolver_handle_to_context(ctx)

    def perform(self, node, inputs, outputs):
        context = inputs[0][0].context

        # Input matrix.
        A = inputs[0]

        assert(len(A.shape) == 2)

        l, n = A.shape
        if l != n:
            raise ValueError('A must be a square matrix')

        lda = max(1, n)

        # cusolver operates on F ordered matrices, but A is expected
        # to be symmetric so it does not matter.
        # We copy A if needed
        if self.inplace:
            L = A
        else:
            L = pygpu.array(A, copy=True)

        # The output matrix will contain only the upper or lower
        # triangular factorization of A. If L is C ordered (it
        # probably is as it is the default in Theano) we just switch
        # the fill mode parameter of cusolver
        l_parameter = 0 if self.lower else 1
        if L.flags['C_CONTIGUOUS']:
            l_parameter = 1 - l_parameter

        L_ptr = L.gpudata

        with context:
            workspace_size = cusolver.cusolverDnSpotrf_bufferSize(
                context.cusolver_handle, l_parameter, n, L_ptr, lda)

            workspace = pygpu.zeros(workspace_size, dtype='float32',
                                    context=context)

            dev_info = pygpu.zeros((1,), dtype='int32', context=context)

            workspace_ptr = workspace.gpudata
            dev_info_ptr = dev_info.gpudata

            cusolver.cusolverDnSpotrf(
                context.cusolver_handle, l_parameter, n, L_ptr, lda, workspace_ptr,
                workspace_size, dev_info_ptr)

            val_dev_info = np.asarray(dev_info)[0]
            if val_dev_info > 0:
                raise LinAlgError('Cholesky decomposition failed (is A SPD?)')

        # cusolver leaves the elements in the matrix outside the considered
        # upper or lower triangle unchanged, so we need to put zeros outside
        # the triangle
        # Note : we should probably check for c or f order in triu instead of here
        if self.lower:
            tril(L)
        else:
            triu(L)

        outputs[0][0] = L
