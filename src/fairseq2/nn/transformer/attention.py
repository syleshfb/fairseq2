# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional, Protocol, Tuple

import torch
import torch.nn.functional as F
from torch import Tensor


class AttentionFunction(Protocol):
    """Computes attention."""

    def __call__(
        self,
        queries: Tensor,
        keys: Tensor,
        values: Tensor,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0.0,
        training: bool = True,
    ) -> Tuple[Tensor, Tensor]:
        """Computes (q, v, k, [m]) attention.

        :param queries:
            The queries. *Shape:* :math:`(N,T,K)`, where :math:`N` is the batch
            size, :math:`T` is the target sequence length, and :math:`K` is the
            key size.
        :param keys:
            The keys. *Shape:* :math:`(N,S,K)`, where :math:`N` is the batch
            size, :math:`S` is the source sequence length, and :math:`K` is the
            key size.
        :param values:
            The values. *Shape:* :math:`(N,S,V)`, where :math:`N` is the batch
            size, :math:`S` is the source sequence length, and :math:`V` is the
            value size.
        :param mask:
            The float mask that will be added to the attention weights before
            computing the attention. *Shape:* :math:`(T,S)` or :math:`(N,T,S)`,
            where :math:`N` is the batch size, :math:`T` is the target sequence
            length, and :math:`S` is the source sequence length.
        :param dropout_p:
            The dropout probability on the attention weights.
        :param training:
            If ``True``, applies dropout.

        :returns:
            - The attentions. *Shape:* :math:`(N,T,V)`, where :math:`N` is the
              batch size, :math:`T` is the target sequence length, and :math:`V`
              is the value size.
            - The attention weights. *Shape:* :math:`(N,T,S)`, where :math:`N`
              is the batch size, :math:`T` is the target sequence length, and
              :math:`S` is the source sequence length.
        """


def scaled_dot_product_attention(
    queries: Tensor,
    keys: Tensor,
    values: Tensor,
    mask: Optional[Tensor] = None,
    dropout_p: float = 0.0,
    training: bool = True,
) -> Tuple[Tensor, Tensor]:
    """Computes (q, v, k, [m]) attention via scaled dot product.

    Computes scaled dot-product attention as described in
    :cite:t:`DBLP:journals/corr/VaswaniSPUJGKP17`.

    :param queries:
        The queries. *Shape:* :math:`(N,T,K)`, where :math:`N` is the batch
        size, :math:`T` is the target sequence length, and :math:`K` is the
        key size.
    :param keys:
        The keys. *Shape:* :math:`(N,S,K)`, where :math:`N` is the batch
        size, :math:`S` is the source sequence length, and :math:`K` is the
        key size.
    :param values:
        The values. *Shape:* :math:`(N,S,V)`, where :math:`N` is the batch
        size, :math:`S` is the source sequence length, and :math:`V` is the
        value size.
    :param mask:
        The float mask that will be added to the attention weights before
        computing the attention. *Shape:* :math:`(T,S)` or :math:`(N,T,S)`,
        where :math:`N` is the batch size, :math:`T` is the target sequence
        length, and :math:`S` is the source sequence length.
    :param dropout_p:
        The dropout probability on the attention weights.
    :param training:
        If ``True``, applies dropout.

    :returns:
        - The attentions. *Shape:* :math:`(N,T,V)`, where :math:`N` is the
          batch size, :math:`T` is the target sequence length, and :math:`V`
          is the value size.
        - The attention weights. *Shape:* :math:`(N,T,S)`, where :math:`N`
          is the batch size, :math:`T` is the target sequence length, and
          :math:`S` is the source sequence length.
    """
    queries = queries * (queries.size(-1) ** -0.5)

    if mask is None:
        # (N, T, K) @ (N, K, S) = (N, T, S)
        attn_weights = torch.bmm(queries, keys.transpose(1, 2))
    else:
        # (N, T, S) + ((N, T, K) @ (N, K, S)) = (N, T, S)
        attn_weights = torch.baddbmm(mask, queries, keys.transpose(1, 2))

    attn_weights = F.softmax(attn_weights, dim=-1)

    if training and dropout_p > 0.0:
        attn_weights = F.dropout(attn_weights, dropout_p, training)

    # (N, T, S) @ (N, S, V) = (N, T, V)
    attn = torch.bmm(attn_weights, values)

    return attn, attn_weights