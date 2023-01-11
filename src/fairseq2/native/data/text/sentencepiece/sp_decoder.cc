// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include "fairseq2/native/data/text/sentencepiece/sp_decoder.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

#include <ATen/Functions.h>
#include <ATen/ScalarType.h>
#include <ATen/Storage.h>
#include <oneapi/tbb.h>

#include "fairseq2/native/memory.h"
#include "fairseq2/native/exception.h"
#include "fairseq2/native/data/text/sentencepiece/sp_model.h"
#include "fairseq2/native/data/text/sentencepiece/sp_processor.h"
#include "fairseq2/native/utils/cast.h"

namespace fairseq2 {
namespace detail {
namespace {

class decoder_op {
public:
    explicit
    decoder_op(const sp_processor *p, at::Tensor &&t);

    std::vector<data> &&
    run() &&;

private:
    void
    decode();

    template <typename T>
    void
    decode();

private:
    const sp_processor *processor_;
    at::Tensor tensor_;
    std::size_t batch_size_;
    std::vector<std::vector<std::string_view>> tokens_{};
    std::vector<data> output_{};
};

}  // namespace
}  // namespace detail

data
sp_decoder::operator()(data &&d) const
{
    if (d.is_tensor())
        d = decode(std::move(d).as_tensor());
    else
        throw std::invalid_argument{
            "The SentencePiece decoder expects as input a tensor."};

    return std::move(d);
}

std::vector<data>
sp_decoder::decode(at::Tensor &&t) const
{
    detail::decoder_op op{&model_->processor(), std::move(t)};

    return std::move(op).run();
}

namespace detail {
namespace {

decoder_op::decoder_op(const sp_processor *p, at::Tensor &&t)
    : processor_{p}, tensor_{std::move(t)}
{
    batch_size_ = static_cast<std::size_t>(tensor_.size(0));

    tokens_.resize(batch_size_);
    output_.resize(batch_size_);
}

std::vector<data> &&
decoder_op::run() &&
{
    tensor_ = tensor_.to(at::kCPU);

    decode();

    return std::move(output_);
}

void
decoder_op::decode()
{
    switch (tensor_.scalar_type()) {
    case at::ScalarType::Short:
        decode<std::int16_t>();
        break;

    case at::ScalarType::Int:
        decode<std::int32_t>();
        break;

    case at::ScalarType::Long:
        decode<std::int64_t>();
        break;

    default:
        throw not_supported_error{
            "The specified integral type is not supported."};
    }
}

template <typename T>
void
decoder_op::decode()
{
    auto seq_dim = static_cast<std::size_t>(tensor_.size(1));

    const at::Storage &s = tensor_.storage();

    memory_span bits{s.unsafe_data<const std::byte>(), s.nbytes()};

    span data = cast<const T>(bits);

    auto op = [this, data, seq_dim](const tbb::blocked_range<std::size_t> &rng) {
        for (auto i = rng.begin(); i < rng.end(); ++i) {
            std::vector<std::string_view> &tokens = tokens_[i];

            span seq_data = data.subspan(i * seq_dim, seq_dim);

            for (T token_idx : seq_data) {
                auto token_idx_32bit = conditional_cast<std::int32_t>(token_idx);

                std::string_view token = processor_->index_to_token(token_idx_32bit);

                tokens.push_back(token);
            }

            output_[i] = processor_->decode(tokens);
        }
    };

    tbb::blocked_range<std::size_t> full_rng{0, batch_size_};

    op(full_rng);
}

}  // namespace
}  // namespace detail
}  // namespace fairseq2