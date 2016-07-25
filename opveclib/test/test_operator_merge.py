# Copyright 2016 Hewlett Packard Enterprise Development LP
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for
# the specific language governing permissions and limitations under the License.

from __future__ import print_function
import unittest
import numpy as np
from sys import _getframe
from ..operator import operator, evaluate, _get_merge_refs_for_op_dag, _build_op_dag, _MergeRef
from ..expression import position_in, output, variable, arange, output_like, if_, elif_, else_, cast
from ..local import clear_op_cache

# Define operators that are used in multiple test cases
@operator()
def add(in0, in1):
    assert in0.shape==in1.shape
    n = in0.shape[0]
    i = position_in(n)
    sumVal = output(n, in0.dtype)
    sumVal[i] = in0[i] + in1[i]
    return sumVal

@operator()
def mul(in0, in1):
    assert in0.shape==in1.shape
    n = in0.shape[0]
    i = position_in(n)
    prodVal = output(n, in0.dtype)
    prodVal[i] = in0[i] * in1[i]
    return prodVal

@operator()
def cumsumRows(in0):
    assert len(in0.shape)==2 # assume 2D
    nRow    = in0.shape[0]
    nCol    = in0.shape[1]
    iCol    = position_in(nCol)
    accum   = variable(0, in0.dtype)
    cumsum  = output_like(in0)
    for iRow in arange(nRow):
        accum <<= accum + in0[iRow, iCol]
        cumsum[iRow, iCol] = accum
    return cumsum

@operator()
def cumsumCols(in0):
    assert len(in0.shape)==2
    nRow    = in0.shape[0]
    nCol    = in0.shape[1]
    iRow    = position_in(nRow)
    accum   = variable(0, in0.dtype)
    cumsum   = output_like(in0)
    for iCol in arange(nCol):
        accum <<= accum + in0[iRow, iCol]
        cumsum[iRow, iCol] = accum
    return cumsum


@operator()
def split(in0):
    assert in0.shape[0] == 4
    nCol = in0.shape[1]
    iCol = position_in(nCol)
    row0 = output(nCol, in0.dtype)
    row1 = output(nCol, in0.dtype)
    row2 = output(nCol, in0.dtype)
    row3 = output(nCol, in0.dtype)
    row0[iCol] = in0[0, iCol]
    row1[iCol] = in0[1, iCol]
    row2[iCol] = in0[2, iCol]
    row3[iCol] = in0[3, iCol]
    return row0, row1, row2, row3


@operator()
def concatenate(op0, op1, op2):
    assert op0.shape == op1.shape
    assert op1.shape == op2.shape
    nCol = op0.shape[0]
    iCol = position_in(nCol)
    merged = output([3, nCol], op0.dtype)
    merged[0, iCol] = op0[iCol]
    merged[1, iCol] = op1[iCol]
    merged[2, iCol] = op2[iCol]
    return merged


@operator()
def merged(in0):
    assert in0.shape[0] == 4
    nCol = in0.shape[1]
    iCol = position_in(nCol)
    merged = output([3, nCol], in0.dtype)
    t0 = in0[0, iCol] + in0[1, iCol]
    t1 = in0[2, iCol] + in0[3, iCol]
    merged[0, iCol] = t0
    merged[1, iCol] = t0 * t1
    merged[2, iCol] = t1
    return merged

@operator()
def matmul(in0, in1):
    assert len(in0.shape) == 2
    assert len(in1.shape) == 2
    assert in0.shape[1] == in1.shape[0] # matrix multiply requires columns of the 1st matrix be equal to the rows of the 2nd matrix
    nRow    = in0.shape[0]
    nCol    = in1.shape[1]
    nWork   = in0.shape[1]
    pos     = position_in([nRow, nCol])
    iRow    = pos[0]
    iCol    = pos[1]
    out     = output([nRow, nCol], in0.dtype)
    accum   = variable(0, dtype=in0.dtype)
    for iWork in arange(nWork):
        accum <<= in0[iRow, iWork] * in1[iWork, iCol]

    out[iRow,iCol] = accum
    return out

# This is wasteful on threads but easy code
@operator()
def pad(in0, n=None):
    assert len(in0.shape) == 2
    nRowIn  = in0.shape[0]
    nColIn  = in0.shape[1]
    nRow    = nRowIn + 2*n
    nCol    = nColIn + 2*n
    out     = output([nRow, nCol], in0.dtype)
    pos     = position_in([nRow, nCol])
    iRow    = pos[0]
    iCol    = pos[1]
    iRowIn  = variable(iRow, dtype=pos.dtype) # Notice we use unsigned data types for indices
    iColIn  = variable(iCol, dtype=pos.dtype)

    with if_(iRowIn<n):
        iRowIn <<= 0
    with elif_(iRowIn>=(nRowIn+n)):
        iRowIn <<= nRowIn-1
    with else_():
        iRowIn <<= iRowIn-n

    with if_(iColIn<n):
        iColIn <<= 0
    with elif_(iColIn>=(nColIn+n)):
        iColIn <<= nColIn-1
    with else_():
        iColIn <<= iColIn-n

    out[iRow, iCol] = in0[iRowIn, iColIn]
    return out

@operator()
def crop(in0, n=None):
    assert len(in0.shape) == 2 # Assume 2D
    assert in0.shape[0] > 2*n # Need to have at least 2n+1 values to be able to remove 2n values
    assert in0.shape[1] > 2*n # DITTO
    nRow    = in0.shape[0] - 2*n
    nCol    = in0.shape[1] - 2*n
    pos     = position_in([nRow, nCol])
    iRow    = pos[0]
    iCol    = pos[1]
    out     = output([nRow, nCol], in0.dtype)
    out[iRow, iCol] = in0[n+iRow, n+iCol]
    return out

# ignores order, it is a comparison between sets
def euqal_set_merge_refs(refs_be, refs_is):
    allSame = len(refs_be) == len(refs_is)
    for ref_be in refs_be:
        if not allSame: break
        foundSame = True
        for ref_is in refs_is:
            foundSame = ref_be.same(ref_is)
            if foundSame: break
        allSame = foundSame
    return allSame


class TestOperator(unittest.TestCase):

    # Tests merging across split and concatenate
    def test_merge_split_concatenate(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)
        in0 = np.random.random([4, 5])              # op-index  in-arg-index  out-arg-index
        row0, row1, row2, row3 = split(in0)         #   0           0           0, 1, 2, 3
        sum0    = add(row0, row1)                   #   1           0, 1        0
        sum1    = add(row2, row3)                   #   2           0, 1        0
        prod0   = mul(sum0, sum1)                   #   3           0, 1        0
        out0    = concatenate(sum0, prod0, sum1)    #   4           0, 1, 2     0
        out1    = merged(in0)
        assert np.allclose(evaluate(out0), evaluate(out1))
        op_dag = _build_op_dag(out0)
        merge_refs = []
        merge_refs.append(_MergeRef(to_op_index=4, to_in_arg_index=0, from_op_index=1, from_out_arg_index=0)) # concatenate-sum0
        merge_refs.append(_MergeRef(to_op_index=4, to_in_arg_index=1, from_op_index=3, from_out_arg_index=0)) # concatenate-prod0
        merge_refs.append(_MergeRef(to_op_index=4, to_in_arg_index=2, from_op_index=2, from_out_arg_index=0)) # concatenate-sum1
        merge_refs.append(_MergeRef(to_op_index=3, to_in_arg_index=0, from_op_index=1, from_out_arg_index=0)) # mul-sum0
        merge_refs.append(_MergeRef(to_op_index=3, to_in_arg_index=1, from_op_index=2, from_out_arg_index=0)) # mul-sum1
        merge_refs.append(_MergeRef(to_op_index=2, to_in_arg_index=0, from_op_index=0, from_out_arg_index=2)) # add-row2
        merge_refs.append(_MergeRef(to_op_index=2, to_in_arg_index=1, from_op_index=0, from_out_arg_index=3)) # add-row3
        merge_refs.append(_MergeRef(to_op_index=1, to_in_arg_index=0, from_op_index=0, from_out_arg_index=0)) # add-row0
        merge_refs.append(_MergeRef(to_op_index=1, to_in_arg_index=1, from_op_index=0, from_out_arg_index=1)) # add-row1
        merge_info = _get_merge_refs_for_op_dag(op_dag)
        assert euqal_set_merge_refs(merge_refs, merge_info.merge_refs)

    def test_multiple_outputs(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)
        in0     = np.random.random([4, 4])  # op-index      in-arg-index    out-arg-index
        mmm0            = matmul(in0, in0)  #   0               0, 1            0
        s0, s1, s2, s3  = split(mmm0)       #   1               0               0, 1, 2, 3
        sum0            = add(s0, s1)       #   2               0, 1            0
        sum1            = add(s2, s3)       #   3               0, 1            0

        op_dag  = _build_op_dag(sum0, sum1)
        merge_info = _get_merge_refs_for_op_dag(op_dag)
        merge_refs = []
        merge_refs.append(_MergeRef(to_op_index=3, to_in_arg_index=0, from_op_index=1, from_out_arg_index=2))  # split-sum1
        merge_refs.append(_MergeRef(to_op_index=3, to_in_arg_index=1, from_op_index=1, from_out_arg_index=3))  # split-sum1
        merge_refs.append(_MergeRef(to_op_index=2, to_in_arg_index=0, from_op_index=1, from_out_arg_index=0))  # split-sum0
        merge_refs.append(_MergeRef(to_op_index=2, to_in_arg_index=1, from_op_index=1, from_out_arg_index=1))  # split-sum1
        assert euqal_set_merge_refs(merge_refs, merge_info.merge_refs)

    # Merging is possible but we need to insert a buffer.
    def test_cumsum_nested(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)
        in0         = np.random.random([3, 5])  # op-index      in-arg-index    out-arg-index
        cRow0       = cumsumRows(in0)           #   0               0               0
        cRow1       = cumsumRows(cRow0)         #   1               0               0
        op_dag      = _build_op_dag(cRow1)
        merge_info  = _get_merge_refs_for_op_dag(op_dag)
        # For cumsumRows(cumsumRows(in0)) we merge using a temporary array for the output of the first cumsumRows!
        merge_refs = []
        merge_refs.append(_MergeRef(to_op_index=1, to_in_arg_index=0, from_op_index=0, from_out_arg_index=0))  # cRow1-cRow0
        assert euqal_set_merge_refs(merge_refs, merge_info.merge_refs)

    # No merging because of different workgroup shapes.
    def test_cumsum(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)
        cRow        = cumsumRows(np.random.random([3, 5]))
        cCol        = cumsumCols(cRow)
        op_dag      = _build_op_dag(cCol)
        merge_info  = _get_merge_refs_for_op_dag(op_dag)

        assert euqal_set_merge_refs([], merge_info.merge_refs)

    # No merging because of different access pattern.
    def test_cumsum_sym(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)
        cRow        = cumsumRows(np.random.random([3, 3]))
        cCol        = cumsumCols(cRow)
        op_dag      = _build_op_dag(cCol)
        merge_info  = _get_merge_refs_for_op_dag(op_dag)

        assert euqal_set_merge_refs([], merge_info.merge_refs)

    # No merging because of different workgroup shapes.
    def test_matmul(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)

        in0     = np.random.random([3, 5])
        in1     = np.random.random([5, 2])
        mmm     = matmul(in0, in1)
        cCol    = cumsumCols(mmm)
        op_dag  = _build_op_dag(cCol)
        merge_info = _get_merge_refs_for_op_dag(op_dag)

        assert euqal_set_merge_refs([], merge_info.merge_refs)

    # No merging because of different workgroup shapes.
    def test_pad_crop(self):
        print('*** Running Test: ' + self.__class__.__name__ + ' function: ' + _getframe().f_code.co_name)

        in0         = np.random.random([3, 5])
        padded      = pad(in0, n=2)
        cropped     = crop(padded, n=2)
        op_dag      = _build_op_dag(cropped)
        merge_info  = _get_merge_refs_for_op_dag(op_dag)

        assert np.allclose(in0, evaluate(cropped))
        assert euqal_set_merge_refs([], merge_info.merge_refs)


if __name__ == '__main__':
    clear_op_cache()
    unittest.main()