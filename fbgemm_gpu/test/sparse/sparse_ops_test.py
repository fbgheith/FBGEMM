#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-ignore-all-errors[56]

import contextlib
import functools
import itertools
import logging
import random
import unittest
from itertools import accumulate
from typing import Any, Callable, cast, Dict, List, Optional, Tuple, Type, Union

import fbgemm_gpu

import hypothesis.strategies as st
import numpy as np
import torch
from hypothesis import given, HealthCheck, settings, Verbosity

from .common import extend_test_class


# pyre-fixme[16]: Module `fbgemm_gpu` has no attribute `open_source`.
open_source: bool = getattr(fbgemm_gpu, "open_source", False)

if open_source:
    # pyre-ignore[21]
    from test_utils import gpu_available, gpu_unavailable
else:
    import fbgemm_gpu.sparse_ops  # noqa: F401, E402
    from fbgemm_gpu.test.test_utils import gpu_available, gpu_unavailable


suppressed_list: List[HealthCheck] = (
    # pyre-fixme[16]: Module `HealthCheck` has no attribute `differing_executors`.
    [HealthCheck.differing_executors]
    if getattr(HealthCheck, "differing_executors", False)
    else []
)


@torch.jit.script
def permute_scripted(
    permute: torch.Tensor, lengths: torch.Tensor, indices: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor]]:
    (
        permuted_lengths_cpu,
        permuted_indices_cpu,
        permuted_weights_cpu,
    ) = torch.ops.fbgemm.permute_2D_sparse_data(permute, lengths, indices, None, None)
    return (
        permuted_lengths_cpu,
        permuted_indices_cpu,
        permuted_weights_cpu,
    )


class SparseOpsTest(unittest.TestCase):
    @staticmethod
    @settings(suppress_health_check=suppressed_list)
    def permute_indices_ref_(
        lengths: torch.Tensor,
        indices: torch.Tensor,
        weights: Optional[torch.Tensor],
        permute: torch.LongTensor,
        is_1D: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor]]:
        T = lengths.size(0)
        B = lengths.size(1)
        if T == 0 or B == 0:
            if is_1D:
                lengths = lengths.view(-1)
            return lengths, indices, weights

        if is_1D:
            permuted_lengths = torch.index_select(lengths.view(-1), 0, permute).view(-1)
            original_segment_lengths = lengths.view(-1)
            original_segment_start = [0] + list(accumulate(lengths.view(-1)))

            permuted_indices = []
            permuted_weights = []
            for i in range(permute.numel()):
                start = original_segment_start[permute[i]]
                end = start + original_segment_lengths[permute[i]]
                permuted_indices.append(indices[start:end])
                if weights is not None:
                    permuted_weights.append(weights[start:end])

            permuted_indices = torch.cat(permuted_indices, dim=0).flatten()

            if weights is None:
                permuted_weights = None
            else:
                permuted_weights = torch.cat(permuted_weights, dim=0).flatten()
        else:
            permuted_lengths = torch.index_select(lengths.view(T, -1), 0, permute)
            original_segment_lengths = lengths.view(T, -1).sum(dim=1, dtype=torch.int32)
            original_segment_start = [0] + list(
                accumulate(original_segment_lengths.view(-1))
            )

            permuted_indices = []
            permuted_weights = []
            for i in range(permute.size(0)):
                start = original_segment_start[permute[i]]
                end = start + original_segment_lengths[permute[i]]
                permuted_indices.append(indices[start:end])
                if weights is not None:
                    permuted_weights.append(weights[start:end])

            permuted_indices = torch.cat(permuted_indices, dim=0).flatten()

            if weights is None:
                permuted_weights = None
            else:
                permuted_weights = torch.cat(permuted_weights, dim=0).flatten()

        return permuted_lengths, permuted_indices, permuted_weights

    @given(
        B=st.integers(min_value=0, max_value=20),
        T=st.integers(min_value=0, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        long_index=st.booleans(),
        has_weight=st.booleans(),
        is_1D=st.booleans(),
        W=st.integers(min_value=4, max_value=8),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_permute_indices(
        self,
        B: int,
        T: int,
        L: int,
        long_index: bool,
        has_weight: bool,
        is_1D: bool,
        W: int,
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        length_splits: Optional[List[torch.Tensor]] = None
        if is_1D:
            if B == 0:
                batch_sizes = [0] * W
            else:
                batch_sizes = [random.randint(a=1, b=B) for i in range(W)]
            length_splits = [
                torch.randint(low=1, high=L, size=(T, batch_sizes[i])).type(index_dtype)
                for i in range(W)
            ]
            lengths = torch.cat(length_splits, dim=1)
        else:
            lengths = torch.randint(low=1, high=L, size=(T, B)).type(index_dtype)

        # pyre-fixme[6]: For 1st param expected `Union[List[int], Size,
        #  typing.Tuple[int, ...]]` but got `Union[bool, float, int]`.
        weights = torch.rand(lengths.sum().item()).float() if has_weight else None
        indices = torch.randint(
            low=1,
            high=int(1e5),
            # pyre-fixme[6]: Expected `Union[int, typing.Tuple[int, ...]]` for 3rd
            #  param but got `Tuple[typing.Union[float, int]]`.
            size=(lengths.sum().item(),),
        ).type(index_dtype)
        if is_1D:
            permute_list = []
            offset_w = [0] + list(
                # pyre-fixme[16]
                accumulate([length_split.numel() for length_split in length_splits])
            )
            for t in range(T):
                for w in range(W):
                    for b in range(batch_sizes[w]):
                        permute_list.append(offset_w[w] + t * batch_sizes[w] + b)
        else:
            permute_list = list(range(T))
            random.shuffle(permute_list)

        permute = torch.IntTensor(permute_list)

        if is_1D:
            (
                permuted_lengths_cpu,
                permuted_indices_cpu,
                permuted_weights_cpu,
            ) = torch.ops.fbgemm.permute_1D_sparse_data(
                permute, lengths, indices, weights, None
            )
        else:
            (
                permuted_lengths_cpu,
                permuted_indices_cpu,
                permuted_weights_cpu,
            ) = torch.ops.fbgemm.permute_2D_sparse_data(
                permute, lengths, indices, weights, None
            )
        (
            permuted_lengths_ref,
            permuted_indices_ref,
            permuted_weights_ref,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, indices, weights, permute.long(), is_1D)
        torch.testing.assert_close(permuted_indices_cpu, permuted_indices_ref)
        torch.testing.assert_close(permuted_lengths_cpu, permuted_lengths_ref)
        if has_weight:
            torch.testing.assert_close(permuted_weights_cpu, permuted_weights_ref)
        else:
            assert permuted_weights_cpu is None and permuted_weights_ref is None

        if gpu_available:
            if is_1D:
                (
                    permuted_lengths_gpu,
                    permuted_indices_gpu,
                    permuted_weights_gpu,
                ) = torch.ops.fbgemm.permute_1D_sparse_data(
                    permute.cuda(),
                    lengths.cuda(),
                    indices.cuda(),
                    # pyre-fixme[16]: `Optional` has no attribute `cuda`.
                    weights.cuda() if has_weight else None,
                    None,
                )
            else:
                (
                    permuted_lengths_gpu,
                    permuted_indices_gpu,
                    permuted_weights_gpu,
                ) = torch.ops.fbgemm.permute_2D_sparse_data(
                    permute.cuda(),
                    lengths.cuda(),
                    indices.cuda(),
                    weights.cuda() if has_weight else None,
                    None,
                )
            torch.testing.assert_close(permuted_indices_gpu.cpu(), permuted_indices_cpu)
            torch.testing.assert_close(permuted_lengths_gpu.cpu(), permuted_lengths_cpu)
            if has_weight:
                torch.testing.assert_close(
                    permuted_weights_gpu.cpu(), permuted_weights_cpu
                )
            else:
                assert permuted_weights_gpu is None

    # TorchScript has different behaviors than eager mode. We can see undefined
    # models returned. So we need to add a unittest to ensure the op return
    # real None, not an undefined tensor.
    def test_permute_indices_scripted_with_none_weights(
        self,
    ) -> None:
        index_dtype = torch.int32
        lengths = torch.randint(low=1, high=2, size=(1, 1)).type(index_dtype)
        weights = None
        indices = torch.randint(
            low=1,
            high=int(1e5),
            # pyre-fixme[6]: Expected `Union[int, typing.Tuple[int, ...]]` for 3rd
            #  param but got `Tuple[typing.Union[float, int]]`.
            size=(lengths.sum().item(),),
        ).type(index_dtype)
        permute_list = list(range(1))
        random.shuffle(permute_list)

        permute = torch.IntTensor(permute_list)

        (
            permuted_lengths_cpu,
            permuted_indices_cpu,
            permuted_weights_cpu,
        ) = permute_scripted(permute, lengths, indices)
        (
            permuted_lengths_ref,
            permuted_indices_ref,
            permuted_weights_ref,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, indices, weights, permute.long(), False)
        self.assertTrue(torch.equal(permuted_indices_cpu, permuted_indices_ref))
        self.assertTrue(torch.equal(permuted_lengths_cpu, permuted_lengths_ref))
        self.assertEqual(permuted_weights_cpu, None)
        self.assertEqual(permuted_weights_ref, None)

    @given(
        permute_size=st.integers(min_value=0, max_value=1000),
        long_index=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_invert_permute(
        self,
        permute_size: int,
        long_index: bool,
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        permute_list = list(range(permute_size))
        random.shuffle(permute_list)
        inversed_permute_list = [0] * len(permute_list)
        for i in range(permute_size):
            inversed_permute_list[permute_list[i]] = i
        permute = torch.IntTensor(permute_list).type(index_dtype)
        inverse_permute_ref = torch.IntTensor(inversed_permute_list).type(index_dtype)

        inverse_permute_cpu = torch.ops.fbgemm.invert_permute(permute)
        torch.testing.assert_close(inverse_permute_cpu, inverse_permute_ref)

        if gpu_available:
            inverse_permute_gpu = torch.ops.fbgemm.invert_permute(permute.cuda())
            torch.testing.assert_close(inverse_permute_gpu.cpu(), inverse_permute_cpu)

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        long_index=st.booleans(),
        has_weight=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=10, deadline=None)
    def test_permute_indices_with_repeats(
        self, B: int, T: int, L: int, long_index: bool, has_weight: bool
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        lengths = torch.randint(low=1, high=L, size=(T, B)).type(index_dtype)
        # pyre-fixme[6]: For 1st param expected `Union[List[int], Size,
        #  typing.Tuple[int, ...]]` but got `Union[bool, float, int]`.
        weights = torch.rand(lengths.sum().item()).float() if has_weight else None
        indices = torch.randint(
            low=1,
            high=int(1e5),
            # pyre-fixme[6]: Expected `Union[int, typing.Tuple[int, ...]]` for 3rd
            #  param but got `Tuple[typing.Union[float, int]]`.
            size=(lengths.sum().item(),),
        ).type(index_dtype)
        permute_list = list(range(T))

        num_repeats = random.randint(0, T)
        for _ in range(num_repeats):
            permute_list.append(random.randint(0, T - 1))

        random.shuffle(permute_list)
        permute = torch.IntTensor(permute_list)

        (
            permuted_lengths_cpu,
            permuted_indices_cpu,
            permuted_weights_cpu,
        ) = torch.ops.fbgemm.permute_2D_sparse_data(permute, lengths, indices, weights)
        (
            permuted_lengths_ref,
            permuted_indices_ref,
            permuted_weights_ref,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, indices, weights, permute.long())
        torch.testing.assert_close(permuted_indices_cpu, permuted_indices_ref)
        torch.testing.assert_close(permuted_lengths_cpu, permuted_lengths_ref)
        if has_weight:
            torch.testing.assert_close(permuted_weights_cpu, permuted_weights_ref)
        else:
            assert permuted_weights_cpu is None and permuted_weights_ref is None

        if gpu_available:
            (
                permuted_lengths_gpu,
                permuted_indices_gpu,
                permuted_weights_gpu,
            ) = torch.ops.fbgemm.permute_2D_sparse_data(
                permute.cuda(),
                lengths.cuda(),
                indices.cuda(),
                # pyre-fixme[16]: `Optional` has no attribute `cuda`.
                weights.cuda() if has_weight else None,
            )
            torch.testing.assert_close(permuted_indices_gpu.cpu(), permuted_indices_cpu)
            torch.testing.assert_close(permuted_lengths_gpu.cpu(), permuted_lengths_cpu)
            if has_weight:
                torch.testing.assert_close(
                    permuted_weights_gpu.cpu(), permuted_weights_cpu
                )
            else:
                assert permuted_weights_cpu is None

    @staticmethod
    def permute_embeddings_(
        permute_fn: Callable[..., Tuple[torch.Tensor, ...]],
        *args: Any,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if permute_fn == torch.ops.fbgemm.permute_2D_sparse_data:
            permuted_lengths, permuted_embeddings, _ = permute_fn(*args, None)
            return permuted_lengths, permuted_embeddings
        else:
            return permute_fn(*args)

    @given(
        B=st.integers(min_value=0, max_value=20),
        T=st.integers(min_value=0, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        long_index=st.booleans(),
        permute_fn=st.sampled_from(
            [
                torch.ops.fbgemm.permute_2D_sparse_data,
                torch.ops.fbgemm.permute_sequence_embeddings,
            ]
        ),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=10, deadline=None)
    def test_permute_embeddings(
        self,
        B: int,
        T: int,
        L: int,
        long_index: bool,
        permute_fn: Callable[..., Tuple[torch.Tensor, ...]],
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        lengths = torch.randint(low=1, high=L, size=(T, B)).type(index_dtype)
        # pyre-fixme[6]: For 1st param expected `Union[List[int], Size,
        #  typing.Tuple[int, ...]]` but got `Union[bool, float, int]`.
        embeddings = torch.rand(lengths.sum().item()).float()
        permute_list = list(range(T))
        random.shuffle(permute_list)
        permute = torch.IntTensor(permute_list)

        (permuted_lengths_cpu, permuted_embeddings_cpu) = self.permute_embeddings_(
            permute_fn, permute, lengths, embeddings
        )
        (
            permuted_lengths_ref,
            permuted_embeddings_ref,
            _,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, embeddings, None, permute.long())
        torch.testing.assert_close(permuted_embeddings_cpu, permuted_embeddings_ref)
        torch.testing.assert_close(permuted_lengths_cpu, permuted_lengths_ref)

        if gpu_available:
            (permuted_lengths_gpu, permuted_embeddings_gpu) = self.permute_embeddings_(
                permute_fn,
                permute.cuda(),
                lengths.cuda(),
                embeddings.cuda(),
            )
            torch.testing.assert_close(
                permuted_embeddings_gpu.cpu(), permuted_embeddings_cpu
            )
            torch.testing.assert_close(permuted_lengths_gpu.cpu(), permuted_lengths_cpu)

    @given(
        n=st.integers(min_value=0, max_value=10),
        long_index=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_cumsum(self, n: int, long_index: bool) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        np_index_dtype = np.int64 if long_index else np.int32

        # cpu tests
        x = torch.randint(low=0, high=100, size=(n,)).type(index_dtype)
        ze = torch.ops.fbgemm.asynchronous_exclusive_cumsum(x)
        zi = torch.ops.fbgemm.asynchronous_inclusive_cumsum(x)
        zc = torch.ops.fbgemm.asynchronous_complete_cumsum(x)
        torch.testing.assert_close(
            torch.from_numpy(np.cumsum(x.cpu().numpy()).astype(np_index_dtype)),
            zi.cpu(),
        )
        torch.testing.assert_close(
            torch.from_numpy(
                (np.cumsum([0] + x.cpu().numpy().tolist())[:-1]).astype(np_index_dtype)
            ),
            ze.cpu(),
        )
        torch.testing.assert_close(
            torch.from_numpy(
                (np.cumsum([0] + x.cpu().numpy().tolist())).astype(np_index_dtype)
            ),
            zc.cpu(),
        )

        # meta tests
        mx = torch.randint(low=0, high=100, size=(n,)).type(index_dtype).to("meta")
        mze = torch.ops.fbgemm.asynchronous_exclusive_cumsum(mx)
        self.assertEqual(ze.size(), mze.size())
        # mzi = torch.ops.fbgemm.asynchronous_inclusive_cumsum(mx)
        # self.assertEqual(zi.size(), mzi.size())
        mzc = torch.ops.fbgemm.asynchronous_complete_cumsum(mx)
        self.assertEqual(zc.size(), mzc.size())

        if gpu_available:
            x = x.cuda()
            ze = torch.ops.fbgemm.asynchronous_exclusive_cumsum(x)
            zi = torch.ops.fbgemm.asynchronous_inclusive_cumsum(x)
            zc = torch.ops.fbgemm.asynchronous_complete_cumsum(x)
            torch.testing.assert_close(
                torch.from_numpy(np.cumsum(x.cpu().numpy()).astype(np_index_dtype)),
                zi.cpu(),
            )
            torch.testing.assert_close(
                torch.from_numpy(
                    (np.cumsum([0] + x.cpu().numpy().tolist())[:-1]).astype(
                        np_index_dtype
                    )
                ),
                ze.cpu(),
            )
            torch.testing.assert_close(
                torch.from_numpy(
                    (np.cumsum([0] + x.cpu().numpy().tolist())).astype(np_index_dtype)
                ),
                zc.cpu(),
            )

    @given(
        n=st.integers(min_value=0, max_value=60),
        b=st.integers(min_value=0, max_value=10),
        long_index=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_asynchronous_complete_cumsum_2d(
        self, n: int, b: int, long_index: bool
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32

        def test_asynchronous_complete_cumsum_2d_helper(x: torch.Tensor) -> None:
            np_index_dtype = np.int64 if long_index else np.int32
            zc = torch.ops.fbgemm.asynchronous_complete_cumsum(x)
            zeros = torch.zeros(b, 1)
            torch.testing.assert_close(
                torch.from_numpy(
                    np.cumsum(
                        torch.concat([zeros, x.cpu()], dim=1).numpy(), axis=1
                    ).astype(np_index_dtype)
                ),
                zc.cpu(),
            )

        x = torch.randint(low=0, high=100, size=(b, n)).type(index_dtype)
        # cpu test
        test_asynchronous_complete_cumsum_2d_helper(x)
        if gpu_available:
            # gpu test
            test_asynchronous_complete_cumsum_2d_helper(x.cuda())

    @given(
        N=st.integers(min_value=1, max_value=20),
        offsets_type=st.sampled_from([torch.int32, torch.int64]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_offsets_range(
        self,
        N: int,
        # pyre-fixme[11]: Annotation `int32` is not defined as a type.
        # pyre-fixme[11]: Annotation `int64` is not defined as a type.
        offsets_type: "Union[Type[torch.int32], Type[torch.int64]]",
    ) -> None:
        lengths = np.array([np.random.randint(low=0, high=20) for _ in range(N)])
        offsets = np.cumsum(np.concatenate(([0], lengths)))[:-1]
        range_ref = torch.from_numpy(
            np.concatenate([np.arange(size) for size in lengths])
        )
        output_size = np.sum(lengths)

        offsets_cpu = torch.tensor(offsets, dtype=offsets_type)
        range_cpu = torch.ops.fbgemm.offsets_range(offsets_cpu, output_size)
        range_ref = range_ref.to(range_cpu.dtype)
        torch.testing.assert_close(range_cpu, range_ref, rtol=0, atol=0)

        if gpu_available:
            range_gpu = torch.ops.fbgemm.offsets_range(offsets_cpu.cuda(), output_size)
            range_ref = range_ref.to(range_gpu.dtype)
            torch.testing.assert_close(range_gpu.cpu(), range_ref, rtol=0, atol=0)

    @given(
        index_type=st.sampled_from([torch.int, torch.long]),
        has_weight=st.booleans(),
        bucketize_pos=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=2, deadline=None)
    def test_bucketize_sparse_features(
        self,
        index_type: Type[torch.dtype],
        has_weight: bool,
        bucketize_pos: bool,
    ) -> None:
        # pyre-ignore [6]
        lengths = torch.tensor([0, 2, 1, 3], dtype=index_type)
        # pyre-ignore [6]
        indices = torch.tensor([10, 10, 15, 20, 25, 30], dtype=index_type)
        weights = (
            torch.tensor([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], dtype=torch.float)
            if has_weight
            else None
        )

        # pyre-ignore [6]
        new_lengths_ref = torch.tensor([0, 2, 0, 2, 0, 0, 1, 1], dtype=index_type)
        # pyre-ignore [6]
        new_indices_ref = torch.tensor([5, 5, 10, 15, 7, 12], dtype=index_type)
        new_weights_ref = torch.tensor(
            [1.0, 2.0, 4.0, 6.0, 3.0, 5.0], dtype=torch.float
        )
        # pyre-ignore [6]
        new_pos_ref = torch.tensor([0, 1, 0, 2, 0, 1], dtype=index_type)
        (
            new_lengths_cpu,
            new_indices_cpu,
            new_weights_cpu,
            new_pos_cpu,
        ) = torch.ops.fbgemm.bucketize_sparse_features(
            lengths, indices, bucketize_pos, 2, weights
        )
        torch.testing.assert_close(new_lengths_cpu, new_lengths_ref, rtol=0, atol=0)
        torch.testing.assert_close(new_indices_cpu, new_indices_ref, rtol=0, atol=0)
        if has_weight:
            torch.testing.assert_close(new_weights_cpu, new_weights_ref)
        if bucketize_pos:
            torch.testing.assert_close(new_pos_cpu, new_pos_ref)
        if gpu_available:
            (
                new_lengths_gpu,
                new_indices_gpu,
                new_weights_gpu,
                new_pos_gpu,
            ) = torch.ops.fbgemm.bucketize_sparse_features(
                lengths.cuda(),
                indices.cuda(),
                bucketize_pos,
                2,
                # pyre-fixme[16]: `Optional` has no attribute `cuda`.
                weights.cuda() if has_weight else None,
            )
            torch.testing.assert_close(
                new_lengths_gpu.cpu(), new_lengths_ref, rtol=0, atol=0
            )
            torch.testing.assert_close(
                new_indices_gpu.cpu(), new_indices_ref, rtol=0, atol=0
            )
            if has_weight:
                torch.testing.assert_close(new_weights_gpu.cpu(), new_weights_cpu)
            if bucketize_pos:
                torch.testing.assert_close(new_pos_gpu.cpu(), new_pos_cpu)

    @unittest.skipIf(*gpu_unavailable)
    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        A=st.integers(min_value=1, max_value=20),
        Dtype=st.sampled_from([torch.int32, torch.float, torch.int64]),
        broadcast_lengths=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_reorder_batched_ad_lengths(
        self,
        B: int,
        T: int,
        L: int,
        A: int,
        Dtype: torch.dtype,
        broadcast_lengths: bool,
    ) -> None:
        if broadcast_lengths:
            cat_ad_lengths = (
                torch.cat([torch.tensor([L for _ in range(T)]) for _ in range(B)], 0)
                .cuda()
                .to(Dtype)
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths.tile([A])
        else:
            cat_ad_lengths = (
                torch.cat(
                    [torch.tensor([L for _ in range(T * A)]) for _ in range(B)], 0
                )
                .cuda()
                .to(Dtype)
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths
        batch_offsets = torch.tensor([A * b for b in range(B + 1)]).int().cuda()
        num_ads_in_batch = B * A
        reordered_batched_ad_lengths = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths, batch_offsets, num_ads_in_batch, broadcast_lengths
        )
        torch.testing.assert_close(
            cat_ad_lengths_broadcasted, reordered_batched_ad_lengths
        )

        cat_ad_lengths_cpu = cat_ad_lengths.cpu()
        batch_offsets_cpu = batch_offsets.cpu()
        reordered_batched_ad_lengths_cpu = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths_cpu, batch_offsets_cpu, num_ads_in_batch, broadcast_lengths
        )
        torch.testing.assert_close(
            reordered_batched_ad_lengths_cpu, reordered_batched_ad_lengths.cpu()
        )

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        A=st.integers(min_value=1, max_value=20),
        Dtype=st.sampled_from([torch.int32, torch.float, torch.int64]),
        broadcast_lengths=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=40, deadline=None)
    def test_reorder_batched_ad_lengths_cpu(
        self,
        B: int,
        T: int,
        L: int,
        A: int,
        Dtype: torch.dtype,
        broadcast_lengths: bool,
    ) -> None:
        if broadcast_lengths:
            cat_ad_lengths = (
                torch.cat([torch.tensor([L for _ in range(T)]) for _ in range(B)], 0)
                .int()
                .to(Dtype)
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths.tile([A])
        else:
            cat_ad_lengths = (
                torch.cat(
                    [torch.tensor([L for _ in range(T * A)]) for _ in range(B)], 0
                )
                .int()
                .to(Dtype)
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths
        batch_offsets = torch.tensor([A * b for b in range(B + 1)]).int()
        num_ads_in_batch = B * A
        reordered_batched_ad_lengths = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths, batch_offsets, num_ads_in_batch, broadcast_lengths
        )
        torch.testing.assert_close(
            cat_ad_lengths_broadcasted, reordered_batched_ad_lengths
        )

    @unittest.skipIf(*gpu_unavailable)
    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        A=st.integers(min_value=1, max_value=20),
        Dtype=st.sampled_from([torch.int32, torch.float, torch.int64, torch.bfloat16]),
        Itype=st.sampled_from([torch.int32, torch.int64]),
        broadcast_indices=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_reorder_batched_ad_indices(
        self,
        B: int,
        T: int,
        L: int,
        A: int,
        Dtype: torch.dtype,
        Itype: torch.dtype,
        broadcast_indices: bool,
    ) -> None:
        if broadcast_indices:
            cat_ad_indices = (
                torch.randint(
                    low=0,
                    high=100,
                    size=(B * T * L,),
                )
                .int()
                .cuda()
                .to(Dtype)
            )
            cat_ad_lengths = (
                torch.cat(
                    [torch.tensor([L for _ in range(T)]) for _ in range(B)],
                    0,
                )
                .int()
                .cuda()
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths.tile([A])
        else:
            cat_ad_indices = (
                torch.randint(
                    low=0,
                    high=100,
                    size=(B * T * A * L,),
                )
                .int()
                .cuda()
                .to(Dtype)
            )
            cat_ad_lengths = (
                torch.cat(
                    [torch.tensor([L for _ in range(T * A)]) for _ in range(B)],
                    0,
                )
                .int()
                .cuda()
            )
            cat_ad_lengths_broadcasted = cat_ad_lengths
        batch_offsets = torch.tensor([A * b for b in range(B + 1)]).int().cuda()
        num_ads_in_batch = B * A
        reordered_cat_ad_lengths = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths, batch_offsets, num_ads_in_batch, broadcast_indices
        )
        torch.testing.assert_close(cat_ad_lengths_broadcasted, reordered_cat_ad_lengths)

        cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            reordered_cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_indices = torch.ops.fbgemm.reorder_batched_ad_indices(
            cat_ad_offsets,
            cat_ad_indices,
            reordered_cat_ad_offsets,
            batch_offsets,
            num_ads_in_batch,
            broadcast_indices,
            B * T * A * L,
        )
        torch.testing.assert_close(
            reordered_cat_ad_indices.view(T, B, A, L).permute(1, 0, 2, 3),
            cat_ad_indices.view(B, T, 1, L).tile([1, 1, A, 1])
            if broadcast_indices
            else cat_ad_indices.view(B, T, A, L),
        )

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        A=st.integers(min_value=1, max_value=20),
        Dtype=st.sampled_from([torch.int32, torch.float, torch.int64]),
        Itype=st.sampled_from([torch.int32, torch.int64]),
        broadcast_indices=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_cat_reorder_batched_ad_indices_cpu(
        self,
        B: int,
        T: int,
        L: int,
        A: int,
        Dtype: torch.dtype,
        Itype: torch.dtype,
        broadcast_indices: bool,
    ) -> None:
        if broadcast_indices:
            ad_indices = [
                (
                    torch.randint(
                        low=0,
                        high=100,
                        size=(T * L,),
                    )
                    .int()
                    .to(Dtype)
                )
                for _ in range(B)
            ]
            cat_ad_lengths = torch.cat(
                [torch.tensor([L for _ in range(T)]) for _ in range(B)],
                0,
            ).int()
            cat_ad_lengths_broadcasted = cat_ad_lengths.tile([A])
            cat_ad_indices = torch.cat(ad_indices, 0)
        else:
            ad_indices = [
                (
                    torch.randint(
                        low=0,
                        high=100,
                        size=(T * A * L,),
                    )
                    .int()
                    .to(Dtype)
                )
                for _ in range(B)
            ]
            cat_ad_lengths = torch.cat(
                [torch.tensor([L for _ in range(T * A)]) for _ in range(B)],
                0,
            ).int()
            cat_ad_lengths_broadcasted = cat_ad_lengths
            cat_ad_indices = torch.cat(ad_indices, 0)
        batch_offsets = torch.tensor([A * b for b in range(B + 1)]).int()
        num_ads_in_batch = B * A
        reordered_cat_ad_lengths = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths, batch_offsets, num_ads_in_batch, broadcast_indices
        )
        torch.testing.assert_close(cat_ad_lengths_broadcasted, reordered_cat_ad_lengths)

        cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            reordered_cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_indices = torch.ops.fbgemm.cat_reorder_batched_ad_indices(
            cat_ad_offsets,
            ad_indices,
            reordered_cat_ad_offsets,
            batch_offsets,
            num_ads_in_batch,
            broadcast_indices,
            B * T * A * L,
        )
        torch.testing.assert_close(
            reordered_cat_ad_indices.view(T, B, A, L).permute(1, 0, 2, 3),
            cat_ad_indices.view(B, T, 1, L).tile([1, 1, A, 1])
            if broadcast_indices
            else cat_ad_indices.view(B, T, A, L),
        )

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        A=st.integers(min_value=1, max_value=20),
        Dtype=st.sampled_from([torch.int32, torch.float, torch.int64]),
        Itype=st.sampled_from([torch.int32, torch.int64]),
        broadcast_indices=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=40, deadline=None)
    def test_reorder_batched_ad_indices_cpu(
        self,
        B: int,
        T: int,
        L: int,
        A: int,
        Dtype: torch.dtype,
        Itype: torch.dtype,
        broadcast_indices: bool,
    ) -> None:
        if broadcast_indices:
            cat_ad_indices = (
                torch.randint(
                    low=0,
                    high=100,
                    size=(B * T * L,),
                )
                .int()
                .to(Dtype)
            )
            cat_ad_lengths = torch.cat(
                [torch.tensor([L for _ in range(T)]) for _ in range(B)],
                0,
            ).int()
            cat_ad_lengths_broadcasted = cat_ad_lengths.tile([A])
        else:
            cat_ad_indices = (
                torch.randint(
                    low=0,
                    high=100,
                    size=(B * T * A * L,),
                )
                .int()
                .to(Dtype)
            )
            cat_ad_lengths = torch.cat(
                [torch.tensor([L for _ in range(T * A)]) for _ in range(B)],
                0,
            ).int()
            cat_ad_lengths_broadcasted = cat_ad_lengths
        batch_offsets = torch.tensor([A * b for b in range(B + 1)]).int()
        num_ads_in_batch = B * A
        reordered_cat_ad_lengths = torch.ops.fbgemm.reorder_batched_ad_lengths(
            cat_ad_lengths, batch_offsets, num_ads_in_batch, broadcast_indices
        )
        torch.testing.assert_close(cat_ad_lengths_broadcasted, reordered_cat_ad_lengths)
        cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            reordered_cat_ad_lengths
        ).to(Itype)
        reordered_cat_ad_indices = torch.ops.fbgemm.reorder_batched_ad_indices(
            cat_ad_offsets,
            cat_ad_indices,
            reordered_cat_ad_offsets,
            batch_offsets,
            num_ads_in_batch,
            broadcast_indices,
            B * T * A * L,
        )
        torch.testing.assert_close(
            reordered_cat_ad_indices.view(T, B, A, L).permute(1, 0, 2, 3),
            cat_ad_indices.view(B, T, 1, L).tile([1, 1, A, 1])
            if broadcast_indices
            else cat_ad_indices.view(B, T, A, L),
        )

    @given(
        B=st.integers(min_value=1, max_value=20),
        R=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        index_dtype=st.sampled_from([torch.int32, torch.int64]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=40, deadline=None)
    def test_reorder_batched_sequence_embeddings_cpu(
        self,
        B: int,
        R: int,
        T: int,
        L: int,
        index_dtype: torch.dtype,
    ) -> None:
        MAX_H = 1000
        DIM = 32
        ref_embeddings = torch.rand(MAX_H, DIM, dtype=torch.float, device="cpu")
        feature_lengths = [
            torch.randint(1, L, (T, random.randint(1, B + 1)), dtype=index_dtype)
            for _ in range(R)
        ]
        feature_indices = [
            torch.randint(
                0, MAX_H, (int(feature_length.sum().item()),), dtype=index_dtype
            )
            for feature_length in feature_lengths
        ]
        cat_feature_indices = torch.cat(feature_indices, 0)
        num_items_in_batch = sum(
            feature_length.size(1) for feature_length in feature_lengths
        )
        num_items_in_batch_list = torch.tensor(
            [feature_length.size(1) for feature_length in feature_lengths],
            dtype=index_dtype,
        )
        embeddings = [
            ref_embeddings[feature_indice] for feature_indice in feature_indices
        ]
        cat_sequence_embeddings = torch.cat(embeddings, 0)
        cat_sequence_embeddings_lengths = torch.cat(
            [feature_length.view(-1) for feature_length in feature_lengths], 0
        ).to(index_dtype)
        cat_sequence_embeddings_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            cat_sequence_embeddings_lengths
        )
        batch_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            num_items_in_batch_list
        )

        reordered_cat_sequence_embeddings_lengths = (
            torch.ops.fbgemm.reorder_batched_ad_lengths(
                cat_sequence_embeddings_lengths, batch_offsets, num_items_in_batch
            )
        )
        reordered_cat_sequence_embeddings_offsets = (
            torch.ops.fbgemm.asynchronous_complete_cumsum(
                reordered_cat_sequence_embeddings_lengths
            )
        )
        reordered_cat_sequence_embeddings = (
            torch.ops.fbgemm.reorder_batched_sequence_embeddings(
                cat_sequence_embeddings_offsets,
                cat_sequence_embeddings,
                reordered_cat_sequence_embeddings_offsets,
                batch_offsets,
                num_items_in_batch,
            )
        )
        reordered_cat_ad_indices = torch.ops.fbgemm.reorder_batched_ad_indices(
            cat_sequence_embeddings_offsets,
            cat_feature_indices,
            reordered_cat_sequence_embeddings_offsets,
            batch_offsets.int(),
            num_items_in_batch,
        )
        reordered_sequence_embedding_from_indices = ref_embeddings[
            reordered_cat_ad_indices
        ]
        torch.testing.assert_close(
            reordered_sequence_embedding_from_indices, reordered_cat_sequence_embeddings
        )

    @given(
        B=st.integers(min_value=1, max_value=20),
        R=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        index_dtype=st.sampled_from([torch.int32, torch.int64]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=40, deadline=None)
    def test_reorder_batched_sequence_embeddings(
        self,
        B: int,
        R: int,
        T: int,
        L: int,
        index_dtype: torch.dtype,
    ) -> None:
        MAX_H = 1000
        DIM = 32
        device = torch.device("cuda")
        ref_embeddings = torch.rand(MAX_H, DIM, dtype=torch.float, device=device)
        feature_lengths = [
            torch.randint(
                1, L, (T, random.randint(1, B + 1)), dtype=index_dtype, device=device
            )
            for _ in range(R)
        ]
        feature_indices = [
            torch.randint(
                0,
                MAX_H,
                (int(feature_length.sum().item()),),
                dtype=index_dtype,
                device=device,
            )
            for feature_length in feature_lengths
        ]
        cat_feature_indices = torch.cat(feature_indices, 0)
        num_items_in_batch = sum(
            feature_length.size(1) for feature_length in feature_lengths
        )
        num_items_in_batch_list = torch.tensor(
            [feature_length.size(1) for feature_length in feature_lengths],
            dtype=index_dtype,
            device=device,
        )
        embeddings = [
            ref_embeddings[feature_indice] for feature_indice in feature_indices
        ]
        cat_sequence_embeddings = torch.cat(embeddings, 0)
        cat_sequence_embeddings_lengths = torch.cat(
            [feature_length.view(-1) for feature_length in feature_lengths], 0
        ).to(index_dtype)
        cat_sequence_embeddings_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            cat_sequence_embeddings_lengths
        )
        batch_offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(
            num_items_in_batch_list
        )

        reordered_cat_sequence_embeddings_lengths = (
            torch.ops.fbgemm.reorder_batched_ad_lengths(
                cat_sequence_embeddings_lengths, batch_offsets.int(), num_items_in_batch
            )
        )
        reordered_cat_sequence_embeddings_offsets = (
            torch.ops.fbgemm.asynchronous_complete_cumsum(
                reordered_cat_sequence_embeddings_lengths
            )
        )
        reordered_cat_sequence_embeddings = (
            torch.ops.fbgemm.reorder_batched_sequence_embeddings(
                cat_sequence_embeddings_offsets,
                cat_sequence_embeddings,
                reordered_cat_sequence_embeddings_offsets,
                batch_offsets,
                num_items_in_batch,
            )
        )
        reordered_cat_ad_indices = torch.ops.fbgemm.reorder_batched_ad_indices(
            cat_sequence_embeddings_offsets,
            cat_feature_indices,
            reordered_cat_sequence_embeddings_offsets,
            batch_offsets.int(),
            num_items_in_batch,
        )
        reordered_sequence_embedding_from_indices = ref_embeddings[
            reordered_cat_ad_indices
        ]
        torch.testing.assert_close(
            reordered_sequence_embedding_from_indices, reordered_cat_sequence_embeddings
        )

    def test_segment_sum_csr(self) -> None:
        segment_sum_cpu = torch.ops.fbgemm.segment_sum_csr(
            2,
            torch.IntTensor([0, 2, 3, 5]),
            torch.Tensor([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]),
        )
        torch.testing.assert_close(
            segment_sum_cpu, torch.Tensor([10.0, 11.0, 34.0]), rtol=0, atol=0
        )
        if torch.cuda.is_available():
            segment_sum_cuda = torch.ops.fbgemm.segment_sum_csr(
                2,
                torch.IntTensor([0, 2, 3, 5]).cuda(),
                torch.Tensor(
                    [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
                ).cuda(),
            )
            torch.testing.assert_close(
                segment_sum_cuda.cpu(), torch.Tensor([10.0, 11.0, 34.0]), rtol=0, atol=0
            )

    @given(
        batch_size=st.just(2),
        m=st.just(3),
        k=st.just(4),
        n=st.just(5),
        use_cpu=st.booleans() if gpu_available else st.just(True),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_permute102_baddbmm_permute102(
        self,
        batch_size: int,
        m: int,
        k: int,
        n: int,
        use_cpu: bool,
    ) -> None:
        # baddbmm doesn't support half
        dtype = torch.float if use_cpu else torch.half
        device = torch.device("cpu" if use_cpu else "cuda")

        A = torch.rand((m, batch_size, k), dtype=dtype, device=device)
        B = torch.rand((batch_size, k, n), dtype=dtype, device=device)
        # bias_permute102 = torch.rand(batch_size, 1, n).half().cuda()
        # bias = bias_permute102.permute(1, 0, 2)

        bias = torch.rand((batch_size, n), dtype=dtype, device=device)
        bias_permute102 = bias.unsqueeze(1)
        # bias = bias_short.unsqueeze(0)

        A_permute102 = A.permute(1, 0, 2)
        C_permute102 = torch.baddbmm(bias_permute102, A_permute102, B)
        C_ref = C_permute102.permute(1, 0, 2)  # (m, batch_size, n)

        C = torch.ops.fbgemm.permute102_baddbmm_permute102(bias, A, B)
        torch.testing.assert_close(C.cpu(), C_ref.cpu())

    @given(
        N=st.integers(1, 32),
        shape=st.one_of(
            st.lists(st.integers(1, 128), max_size=1),
            st.lists(st.integers(1, 16), min_size=2, max_size=2),
        ),
        dtype=st.sampled_from([torch.float, torch.half, torch.double]),
        use_cpu=st.booleans() if gpu_available else st.just(True),
        consecutive_indices=st.booleans(),
        skip_indices_sorting_fwd=st.booleans(),
        use_inference_mode=st.booleans(),
    )
    @settings(max_examples=20, deadline=None)
    def test_index_select_dim0(
        self,
        N: int,
        shape: List[int],
        dtype: torch.dtype,
        use_cpu: bool,
        consecutive_indices: bool,
        skip_indices_sorting_fwd: bool,
        use_inference_mode: bool,
    ) -> None:
        device = torch.device("cpu" if use_cpu else "cuda")
        U = random.randint(0, N + 1)

        kwargs = {}
        if consecutive_indices:
            start = np.random.randint(0, U)
            length = np.random.randint(1, U - start + 1)
            indices = list(range(start, start + length))
            np_arr = np.array(indices)
            for _ in range(N - U):
                indices.append(np.random.randint(start, start + length))
                np_arr = np.array(indices)
                np.random.shuffle(np_arr)
            indices = torch.from_numpy(np_arr).to(torch.int).to(device)
            kwargs["consecutive_range_start"] = start
            kwargs["consecutive_range_length"] = length
        else:
            indices = torch.randint(U, (N,), device=device)

        kwargs["skip_indices_sorting_fwd"] = skip_indices_sorting_fwd

        input = torch.rand((U,) + tuple(shape), dtype=dtype, device=device)

        with torch.inference_mode() if use_inference_mode else contextlib.nullcontext():
            output_ref = torch.ops.fbgemm.index_select_dim0(input, indices, **kwargs)
            output = torch.index_select(input, 0, indices)

            torch.testing.assert_close(output, output_ref)

        if not use_inference_mode:
            gradcheck_args = [
                input.clone().detach().double().requires_grad_(True),
                indices,
            ]
            for k in kwargs:
                gradcheck_args.append(kwargs[k])

            torch.autograd.gradcheck(torch.ops.fbgemm.index_select_dim0, gradcheck_args)

    @given(
        num_indices=st.integers(1, 32),
        max_num_input_rows=st.integers(1, 32),
        shape=st.lists(st.integers(1, 32), min_size=1, max_size=2),
        dtype=st.sampled_from([torch.float, torch.half, torch.double]),
        use_cpu=st.booleans() if gpu_available else st.just(True),
        num_groups=st.integers(1, 32),
        use_var_cols=st.booleans(),
        use_var_num_input_rows=st.booleans(),
        check_non_contiguous=st.booleans(),
    )
    @settings(
        verbosity=Verbosity.verbose,
        max_examples=20,
        deadline=None,
    )
    def test_group_index_select_dim0(
        self,
        num_indices: int,
        max_num_input_rows: int,
        shape: List[int],
        dtype: torch.dtype,
        use_cpu: bool,
        num_groups: int,
        use_var_cols: bool,
        use_var_num_input_rows: bool,
        check_non_contiguous: bool,
    ) -> None:
        device = torch.device("cpu" if use_cpu else "cuda")

        input_group: List[torch.Tensor] = []
        input_ref_group: List[torch.Tensor] = []
        indices_group: List[torch.Tensor] = []
        grad_group: List[torch.Tensor] = []
        for _ in range(num_groups):
            if use_var_num_input_rows:
                num_input_rows = (
                    random.randint(1, max_num_input_rows)
                    if max_num_input_rows > 1
                    else 1
                )
            else:
                num_input_rows = max_num_input_rows
            indices = torch.randint(num_input_rows, (num_indices,), device=device)
            assert indices.max() < num_input_rows

            if use_var_cols:
                var_dim = random.randint(0, len(shape) - 1)
                new_shape = random.randint(1, 32)
                shape[var_dim] = new_shape
            indices_group.append(indices)
            input = torch.rand(
                (num_input_rows,) + tuple(shape), dtype=dtype, device=device
            )
            input_ref = input.clone().detach()

            input.requires_grad = True
            input_ref.requires_grad = True

            input_group.append(input)
            input_ref_group.append(input_ref)

            grad = torch.rand((num_indices,) + tuple(shape), dtype=dtype, device=device)
            grad_group.append(grad)

        # Test forward
        output_ref_group = []
        for input, indices in zip(input_ref_group, indices_group):
            output_ref_group.append(torch.index_select(input, 0, indices))

        output_group = torch.ops.fbgemm.group_index_select_dim0(
            input_group, indices_group
        )

        # Test backward
        for out, grad in zip(output_ref_group, grad_group):
            out.backward(grad)

        cat_output = torch.concat(
            [
                (
                    # Transpose is likely going to make the tensor
                    # noncontiguous
                    output.transpose(1, 0).flatten()
                    if check_non_contiguous
                    else output.flatten()
                )
                for output in output_group
            ]
        )

        cat_grad = torch.concat(
            [
                (
                    # Transpose is likely going to make the tensor
                    # noncontiguous
                    grad.transpose(1, 0).flatten()
                    if check_non_contiguous
                    else grad.flatten()
                )
                for grad in grad_group
            ]
        )
        cat_output.backward(cat_grad)

        def compare_tensor_groups(
            test_group: List[torch.Tensor],
            ref_group: List[torch.Tensor],
            tensor_type: str,
            tols: Dict["str", float],
        ) -> None:
            passed = True
            failure_count = 0
            for i, (test, ref) in enumerate(zip(test_group, ref_group)):
                # pyre-ignore [6]
                if not torch.allclose(test, ref, **tols):
                    passed = False
                    failure_count += 1
                    print(
                        f"FAILED: group {i} {tensor_type} ({dtype}), "
                        f"input shape {input_group[i].shape}, indices "
                        f"{indices_group[i]}, test {test}, ref {ref}"
                    )
            assert (
                passed
            ), f"{failure_count}/{num_groups} groups of {tensor_type} failed"

        compare_tensor_groups(
            output_group, output_ref_group, "activation", {"rtol": 0, "atol": 0}
        )
        compare_tensor_groups(
            # pyre-ignore [6]
            [i.grad for i in input_group],
            # pyre-ignore [6]
            [i.grad for i in input_ref_group],
            "gradient",
            {"rtol": 1e-02, "atol": 1e-02} if dtype == torch.half else {},
        )

    @given(
        T=st.integers(1, 5),
        B=st.integers(1, 5),
        L=st.integers(1, 5),
    )
    @settings(max_examples=20, deadline=None)
    def test_bottom_unique_k_per_row(
        self,
        T: int,
        B: int,
        L: int,
    ) -> None:
        E = 1000000
        all_indices = (np.random.zipf(a=1.15, size=(T, B, 3 * L)) - 1) % E
        all_indices_deduped = torch.ops.fbgemm.bottom_k_per_row(
            torch.as_tensor(all_indices), torch.tensor([0, L], dtype=torch.long), True
        )
        for index_tuple in itertools.product(range(T), range(B)):
            # sample without replacement from
            # https://stats.stackexchange.com/questions/20590/how-do-i-sample-without-replacement-using-a-sampling-with-replacement-function
            r = set()
            for x in all_indices[index_tuple]:
                if x not in r:
                    r.add(x)
                    if len(r) == L:
                        break
            assert (len(r)) == L, "too skewed distribution (alpha too big)"
            all_indices[index_tuple][:L] = sorted(r)
        all_indices_deduped_ref = torch.as_tensor(all_indices[:, :, :L])
        torch.testing.assert_close(all_indices_deduped, all_indices_deduped_ref)

    @given(
        num_inputs=st.integers(0, 100),
        max_input_rows=st.integers(2, 32),
        max_cols_factor=st.integers(2, 256),
        max_output_rows=st.integers(2, 32),
        permute_output_dim_0_1=st.booleans(),
        dtype=st.sampled_from([torch.float, torch.half]),
        use_cpu=st.booleans() if gpu_available else st.just(True),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=20, deadline=None)
    def test_batch_index_select_dim0(  # noqa: C901
        self,
        num_inputs: int,
        max_input_rows: int,
        max_cols_factor: int,
        max_output_rows: int,
        permute_output_dim_0_1: bool,
        dtype: torch.dtype,
        use_cpu: bool,
    ) -> None:
        device = "cpu" if use_cpu else "cuda"
        input_rows = torch.randint(
            low=1, high=max_input_rows, size=(num_inputs,)
        ).tolist()
        input_columns = (
            torch.randint(low=1, high=max_cols_factor, size=(num_inputs,)) * 4
        ).tolist()
        if permute_output_dim_0_1:
            # All num_indices must be the same if permute_output_dim_0_1 is
            # True
            num_indices = torch.randint(low=1, high=max_output_rows, size=(1,)).item()
            input_num_indices = [num_indices] * num_inputs
        else:
            input_num_indices = torch.randint(
                low=1, high=max_output_rows, size=(num_inputs,)
            ).tolist()

        def validate(
            test_list: List[torch.Tensor],
            ref_list: List[torch.Tensor],
            rows: List[int],
            val_fn: Callable[[torch.Tensor, torch.Tensor], bool],
            name: str,
        ) -> None:
            test_passed_all = True
            error_msg = ""
            for i, (test, ref) in enumerate(zip(test_list, ref_list)):
                test = test.float()
                ref = ref.float()
                test_passed = val_fn(test, ref)
                test_passed_all = test_passed & test_passed_all
                if not test_passed:
                    test = test.reshape(rows[i], -1)
                    ref = ref.reshape(rows[i], -1)
                    for r in range(rows[i]):
                        test_row = test[r]
                        ref_row = ref[r]
                        if not val_fn(test_row, ref_row):
                            error_msg += f"ERROR: {name} {i} row {r} are different, test {test_row}, ref {ref_row}\n"
            assert test_passed_all, error_msg
            logging.info(f"{name} test passed")

        if num_inputs == 0:
            inputs = [torch.empty(0, dtype=dtype, device=device)]
            indices = [torch.empty(0, dtype=torch.long, device=device)]
        else:
            inputs = [
                torch.rand(rows, cols, dtype=dtype, device=device)
                for rows, cols in zip(input_rows, input_columns)
            ]
            indices = [
                torch.randint(
                    low=0, high=rows, size=(num,), dtype=torch.long, device=device
                )
                for num, rows in zip(input_num_indices, input_rows)
            ]

        for i in range(len(inputs)):
            inputs[i].requires_grad = True

        output_ref = [
            input.index_select(dim=0, index=index).flatten()
            for input, index in zip(inputs, indices)
        ]

        concat_inputs = torch.concat(
            [input.flatten().clone().detach() for input in inputs]
        )
        concat_indices = torch.concat(indices)

        concat_inputs.requires_grad = True

        output_test = torch.ops.fbgemm.batch_index_select_dim0(
            concat_inputs,
            concat_indices,
            input_num_indices,
            input_rows,
            input_columns,
            permute_output_dim_0_1,
        )

        if permute_output_dim_0_1 and num_inputs > 0:
            output_list = output_test.view(input_num_indices[0], -1).split(
                input_columns,
                dim=1,
            )
            output_list = [out.flatten() for out in output_list]
        else:
            output_list = output_test.split(
                [rows * cols for rows, cols in zip(input_num_indices, input_columns)]
            )

        validate(output_list, output_ref, input_num_indices, torch.equal, "output")

        if num_inputs == 0:
            grads = [torch.empty(0, dtype=dtype, device=device)]
        else:
            grads = [torch.rand_like(output) for output in output_ref]
        for out_ref, grad in zip(output_ref, grads):
            out_ref.backward(grad)

        if permute_output_dim_0_1 and num_inputs > 0:
            concat_grads = torch.concat(
                [grad.view(input_num_indices[0], -1) for grad in grads], dim=1
            ).flatten()
        else:
            concat_grads = torch.concat(grads)

        assert concat_grads.shape == output_test.shape
        output_test.backward(concat_grads)

        assert concat_inputs.grad is not None
        grad_list = concat_inputs.grad.split(
            [rows * cols for rows, cols in zip(input_rows, input_columns)]
        )

        grad_ref = []
        for input in inputs:
            assert input.grad is not None
            grad_ref.append(input.grad.flatten())

        tol = 1.0e-4 if dtype == torch.float else 1.0e-2

        validate(
            grad_list,
            grad_ref,
            input_rows,
            functools.partial(torch.allclose, atol=tol, rtol=tol),
            "grad",
        )

    def permute_sparse_features_ref_(
        self,
        lengths: torch.Tensor,
        indices: torch.Tensor,
        weights: Optional[torch.Tensor],
        permute: torch.LongTensor,
    ) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor]]:
        T = lengths.size(0)
        B = lengths.size(1)
        permuted_lengths = torch.index_select(lengths.view(T, B), 0, permute)

        original_segment_lengths = lengths.view(T, B).sum(dim=1, dtype=torch.int32)
        original_segment_start = torch.ops.fbgemm.asynchronous_exclusive_cumsum(
            original_segment_lengths.view(-1)
        )

        permuted_indices = []
        permuted_weights = []
        for i in range(permute.size(0)):
            start = original_segment_start[permute[i]]
            end = start + original_segment_lengths[permute[i]]
            permuted_indices.append(indices[start:end])
            if weights is not None:
                permuted_weights.append(weights[start:end])

        permuted_indices = torch.cat(permuted_indices, dim=0).flatten()

        if weights is None:
            permuted_weights = None
        else:
            permuted_weights = torch.cat(permuted_weights, dim=0).flatten()

        return permuted_lengths, permuted_indices, permuted_weights

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        long_index=st.booleans(),
        has_weight=st.booleans(),
    )
    @settings(max_examples=20, deadline=None)
    def test_permute_sparse_features(
        self, B: int, T: int, L: int, long_index: bool, has_weight: bool
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        lengths = torch.randint(low=1, high=L, size=(T, B)).type(index_dtype)
        weights = torch.rand(int(lengths.sum().item())).float() if has_weight else None
        indices = torch.randint(
            low=1,
            high=int(1e5),
            size=cast(Tuple[int, ...], (lengths.sum().item(),)),
        ).type(index_dtype)
        permute_list = list(range(T))
        random.shuffle(permute_list)
        permute = torch.IntTensor(permute_list)

        (
            permuted_lengths_cpu,
            permuted_indices_cpu,
            permuted_weights_cpu,
        ) = torch.ops.fbgemm.permute_sparse_features(permute, lengths, indices, weights)
        (
            permuted_lengths_ref,
            permuted_indices_ref,
            permuted_weights_ref,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, indices, weights, permute.long())
        torch.testing.assert_close(permuted_indices_cpu, permuted_indices_ref)
        torch.testing.assert_close(permuted_lengths_cpu, permuted_lengths_ref)
        if has_weight:
            torch.testing.assert_close(permuted_weights_cpu, permuted_weights_ref)
        else:
            assert permuted_weights_cpu is None and permuted_weights_ref is None

        if gpu_available:
            (
                permuted_lengths_gpu,
                permuted_indices_gpu,
                permuted_weights_gpu,
            ) = torch.ops.fbgemm.permute_sparse_features(
                permute.cuda(),
                lengths.cuda(),
                indices.cuda(),
                weights.cuda() if has_weight and weights is not None else None,
            )
            torch.testing.assert_close(permuted_indices_gpu.cpu(), permuted_indices_cpu)
            torch.testing.assert_close(permuted_lengths_gpu.cpu(), permuted_lengths_cpu)
            if has_weight:
                torch.testing.assert_close(
                    permuted_weights_gpu.cpu(), permuted_weights_cpu
                )
            else:
                assert permuted_weights_gpu is None

    @given(
        B=st.integers(min_value=1, max_value=20),
        T=st.integers(min_value=1, max_value=20),
        L=st.integers(min_value=2, max_value=20),
        long_index=st.booleans(),
        has_weight=st.booleans(),
    )
    @settings(max_examples=20, deadline=None)
    def test_permute_sparse_features_with_repeats(
        self, B: int, T: int, L: int, long_index: bool, has_weight: bool
    ) -> None:
        index_dtype = torch.int64 if long_index else torch.int32
        lengths = torch.randint(low=1, high=L, size=(T, B)).type(index_dtype)
        weights = torch.rand(int(lengths.sum().item())).float() if has_weight else None
        indices = torch.randint(
            low=1,
            high=int(1e5),
            size=cast(Tuple[int, ...], (lengths.sum().item(),)),
        ).type(index_dtype)
        permute_list = list(range(T))

        num_repeats = random.randint(0, T)
        for _ in range(num_repeats):
            permute_list.append(random.randint(0, T - 1))

        random.shuffle(permute_list)
        permute = torch.IntTensor(permute_list)

        (
            permuted_lengths_cpu,
            permuted_indices_cpu,
            permuted_weights_cpu,
        ) = torch.ops.fbgemm.permute_sparse_features(permute, lengths, indices, weights)
        (
            permuted_lengths_ref,
            permuted_indices_ref,
            permuted_weights_ref,
            # pyre-fixme[6]: For 4th param expected `LongTensor` but got `Tensor`.
        ) = self.permute_indices_ref_(lengths, indices, weights, permute.long())
        torch.testing.assert_close(permuted_indices_cpu, permuted_indices_ref)
        torch.testing.assert_close(permuted_lengths_cpu, permuted_lengths_ref)
        if has_weight:
            torch.testing.assert_close(permuted_weights_cpu, permuted_weights_ref)
        else:
            assert permuted_weights_cpu is None and permuted_weights_ref is None

        if gpu_available:
            (
                permuted_lengths_gpu,
                permuted_indices_gpu,
                permuted_weights_gpu,
            ) = torch.ops.fbgemm.permute_sparse_features(
                permute.cuda(),
                lengths.cuda(),
                indices.cuda(),
                weights.cuda() if has_weight and weights is not None else None,
            )
            torch.testing.assert_close(permuted_indices_gpu.cpu(), permuted_indices_cpu)
            torch.testing.assert_close(permuted_lengths_gpu.cpu(), permuted_lengths_cpu)
            if has_weight:
                torch.testing.assert_close(
                    permuted_weights_gpu.cpu(), permuted_weights_cpu
                )
            else:
                assert permuted_weights_cpu is None


# e.g. "test_faketensor__test_cumsum": [unittest.expectedFailure]
# Please avoid putting tests here, you should put operator-specific
# skips and failures in deeplearning/fbgemm/fbgemm_gpu/test/failures_dict.json
# pyre-ignore[24]: Generic type `Callable` expects 2 type parameters.
additional_decorators: Dict[str, List[Callable]] = {
    "test_aot_dispatch_dynamic__test_index_select_dim0": [unittest.skip("hangs")],
    "test_aot_dispatch_static__test_index_select_dim0": [unittest.skip("hangs")],
    "test_faketensor__test_index_select_dim0": [unittest.skip("hangs")],
    "test_autograd_registration__test_index_select_dim0": [unittest.skip("hangs")],
    "test_schema__test_index_select_dim0": [unittest.skip("hangs")],
}

extend_test_class(SparseOpsTest, additional_decorators)

if __name__ == "__main__":
    unittest.main()