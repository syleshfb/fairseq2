# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from itertools import islice

import pytest
import torch

from fairseq2.data import read_sequence
from tests.common import tmp_rng_seed


class TestShuffleOp:
    def test_op_works_as_expected(self) -> None:
        cpu = torch.device("cpu")

        seq = range(1, 10)

        dp = read_sequence(seq).shuffle(100).and_return()

        for _ in range(2):
            with tmp_rng_seed(cpu, seed=2):
                assert list(dp) == [7, 1, 3, 2, 6, 5, 8, 4, 9]

                dp.reset()

        dp = read_sequence(seq).shuffle(0).and_return()

        for _ in range(2):
            with tmp_rng_seed(cpu, seed=2):
                assert list(dp) == [7, 1, 3, 2, 6, 5, 8, 4, 9]

                dp.reset()

        dp = read_sequence(seq).shuffle(3).and_return()

        for _ in range(2):
            with tmp_rng_seed(cpu, seed=2):
                assert list(dp) == [1, 3, 5, 2, 6, 7, 4, 9, 8]

                dp.reset()

        dp = read_sequence(seq).shuffle(1).and_return()

        for _ in range(2):
            with tmp_rng_seed(cpu, seed=2):
                assert list(dp) == [1, 2, 3, 4, 5, 6, 7, 8, 9]

                dp.reset()

    @pytest.mark.parametrize("shuffle_window", [10, 100, 1000])
    def test_record_reload_position_works_as_expected(
        self, shuffle_window: int
    ) -> None:
        cpu = torch.device("cpu")

        dp1 = read_sequence(list(range(5000))).shuffle(shuffle_window).and_return()
        dp2 = read_sequence(list(range(5000))).shuffle(shuffle_window).and_return()

        with tmp_rng_seed(cpu, seed=2):
            expected_output1 = list(islice(dp1, 4000))

        with tmp_rng_seed(cpu, seed=3):
            expected_output2 = list(islice(dp1, 1000))

        with tmp_rng_seed(cpu, seed=2):
            assert list(islice(dp2, 4000)) == expected_output1

        state_dict = dp2.state_dict()

        with tmp_rng_seed(cpu, seed=3):
            assert list(islice(dp2, 1000)) == expected_output2

        dp2.load_state_dict(state_dict)

        with tmp_rng_seed(cpu, seed=3):
            assert list(islice(dp2, 1000)) == expected_output2

        dp2.reset()
        dp2.load_state_dict(state_dict)

        with tmp_rng_seed(cpu, seed=3):
            assert list(islice(dp2, 1000)) == expected_output2

        state_dict = dp2.state_dict()

        with pytest.raises(StopIteration):
            next(iter(dp2))

        dp2.reset()

        dp2.load_state_dict(state_dict)

        with pytest.raises(StopIteration):
            next(iter(dp2))

    def test_record_reload_position_works_as_expected_with_no_strict(self) -> None:
        dp = read_sequence(list(range(100))).shuffle(80, strict=False).and_return()

        # Do one dummy iteration to force to fill the buffer.
        next(iter(dp))

        state_dict = dp.state_dict()

        dp.load_state_dict(state_dict)

        assert min(dp) == 81