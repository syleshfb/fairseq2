from pathlib import Path
from typing import Any, Dict, List, Sequence

import sentencepiece
import torch
from torch import Tensor


class Tokenizer:

    UNK = 0
    BOS = 1
    EOS = 2
    PAD = 3

    def __init__(self) -> None:
        self.special_tokens: Dict[str, int] = {}

    def vocab_size(self) -> int:
        raise NotImplementedError

    def encode_batch(self, sentences: Sequence[str], bos: int = -1) -> Tensor:
        raise NotImplementedError

    def decode_batch(self, tokens: Tensor) -> List[str]:
        raise NotImplementedError

    def num_tokens(self, tokens: Tensor) -> int:
        return int((tokens != self.PAD).sum())

    def add_special_token(self, token: str, idx: int = -1) -> int:
        if token in self.special_tokens:
            n = self.special_tokens[token]
            if idx >= 0:
                assert (
                    idx == n
                ), f"{token} is already assigned to {n}, can't remap to {idx}"
            return n

        n = idx if idx >= 0 else self.vocab_size()
        self.special_tokens[token] = n
        return n


class SpmTokenizer(Tokenizer):
    @staticmethod
    def from_file(file: Path, _pad_shift_hack: bool = False) -> "SpmTokenizer":
        spm = sentencepiece.SentencePieceProcessor()
        spm.load(str(file))
        return SpmTokenizer(spm, _pad_shift_hack=_pad_shift_hack)

    def __init__(
        self,
        spm: sentencepiece.SentencePieceProcessor,
        sampling: bool = False,
        _pad_shift_hack: bool = False,
    ):
        super().__init__()
        self.spm = spm
        self.sampling = sampling
        # HACK to reproduce Fairseq1 behavior
        # Fairseq1 is not using tokens returned by spm, but convert them to string then back to index.
        # The results is to shift each word token by one.
        self._pad_shift_hack = _pad_shift_hack

        # Typically UNK = 0, BOS = 1, EOS = 2, PAD = VOCAB_SIZE
        # With _pad_shift_hack: PAD = 0, UNK =1, BOS = 2, EOS = 3
        self.UNK = self.add_special_token("<UNK>", spm.unk_id() + _pad_shift_hack)
        self.BOS = self.add_special_token("<BOS>", spm.bos_id() + _pad_shift_hack)
        self.EOS = self.add_special_token("<EOS>", spm.eos_id() + _pad_shift_hack)
        self.PAD = self.add_special_token("<PAD>", 0 if _pad_shift_hack else -1)

    def state_dict(self) -> Dict[str, Any]:
        state = dict(self.__dict__)
        state["spm"] = self.spm.serialized_model_proto()
        return state

    def load_state_dict(self, state: Dict[str, Any]) -> None:
        spm = sentencepiece.SentencePieceProcessor()
        spm.load_from_serialized_proto(state["spm"])
        state["spm"] = spm
        self.__dict__.update(state)

    def vocab_size(self) -> int:
        # unk, bos, and eos are both in spm.GetPieceSize() and special_tokens
        return int(self.spm.GetPieceSize()) - 3 + len(self.special_tokens)

    def encode_batch(self, sentences: Sequence[str], bos: int = -1) -> Tensor:
        tokens: List[List[int]] = [
            self.spm.encode_as_ids(
                # TODO: the sampling should be configurable
                sentence,
                add_bos=True,
                add_eos=True,
                enable_sampling=self.sampling,
            )
            for sentence in sentences
        ]
        bos = self.BOS if bos < 0 else bos
        return _make_batch(
            tokens,
            self.PAD,
            rewrite_bos=bos,
            shift_tokens=1 if self._pad_shift_hack else 0,
        )

    def decode_batch(self, tokens: Tensor) -> List[str]:
        # Replace special tokens with BOS.
        # TODO: allow to print special tokens (again, we should probably modify the underlying spm)
        tokens = tokens.clone().detach()
        tokens[tokens >= self.spm.GetPieceSize()] = self.BOS
        # SPM doesn't now PAD.
        tokens[tokens == self.PAD] = self.EOS
        if self._pad_shift_hack:
            tokens = tokens - 1

        return [self._decode(tokens[i, :].tolist()) for i in range(tokens.size(0))]

    def _decode(self, tokens: List[int]) -> str:
        # TODO: encode
        if tokens[-1] == self.PAD:
            first_pad = tokens.index(self.PAD)
        else:
            first_pad = len(tokens)
        return self.spm.decode(tokens[:first_pad])  # type: ignore


class DictTokenizer(Tokenizer):
    """Dict and spaces based tokenizer like in legacy Fairseq."""

    @staticmethod
    def from_fairseq_dict_txt(file: Path) -> "DictTokenizer":
        import fairseq.data

        # TODO: read the file ourselves
        src_dict = fairseq.data.Dictionary.load(str(file))
        src_dict.indices["<UNK>"] = src_dict.unk_index
        src_dict.indices["<BOS>"] = src_dict.bos_index
        src_dict.indices["<EOS>"] = src_dict.eos_index
        src_dict.indices["<PAD>"] = src_dict.pad_index

        return DictTokenizer(src_dict.indices, src_dict.tokens)

    @staticmethod
    def from_vocab(vocab: List[str]) -> "DictTokenizer":
        """Makes a DictTokenizer from a list of words.

        Note that the 4 special tokens would always prepended.
        """
        # TODO: make a C++ implementation of this
        special_tokens = ["<UNK>", "<BOS>", "<EOS>", "<PAD>"]
        vocab = special_tokens + vocab
        indices = {word: idx for idx, word in enumerate(vocab)}
        return DictTokenizer(indices, vocab)

    def __init__(self, indices: Dict[str, int], vocab: List[str]):
        super().__init__()
        self.indices = indices
        self.vocab = vocab  # equivalent to "self.tokens" in Fairseq Dictionary
        self.UNK = self.add_special_token("<UNK>", self.indices["<UNK>"])
        self.BOS = self.add_special_token("<BOS>", self.indices["<BOS>"])
        self.EOS = self.add_special_token("<EOS>", self.indices["<EOS>"])
        self.PAD = self.add_special_token("<PAD>", self.indices["<PAD>"])

    def state_dict(self) -> Dict[str, Any]:
        return self.__dict__

    def load_state_dict(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)

    def vocab_size(self) -> int:
        return len(self.vocab)

    def encode_batch(self, sentences: Sequence[str], bos: int = -1) -> Tensor:
        bos = self.BOS if bos < 0 else bos
        tokens = [self._encode(sentence, bos) for sentence in sentences]
        return _make_batch(tokens, self.PAD)

    def _encode(self, sentence: str, bos: int) -> List[int]:
        tokens = [bos]
        UNK = self.UNK
        for word in sentence.split():
            tokens.append(self.indices.get(word, UNK))
        tokens.append(self.EOS)
        return tokens

    def decode_batch(self, tokens: Tensor) -> List[str]:
        return [self._decode(tokens[i, :].tolist()) for i in range(tokens.size(0))]

    def _decode(self, tokens: List[int]) -> str:
        return " ".join(
            self.vocab[t] for t in tokens if t not in (self.PAD, self.EOS, self.BOS)
        )


# TODO do this in C++
def _make_batch(
    values: List[List[int]],
    pad_id: int,
    pad_to_length: int = 0,
    pad_to_multiple: int = 1,
    batch_size: int = 0,
    left_pad: bool = False,
    # TODO: use int16 when possible
    dtype: torch.dtype = torch.int64,
    rewrite_bos: int = -1,
    prepend_bos: int = -1,
    shift_tokens: int = 0,
) -> Tensor:
    """Convert a list of token-index list into a padded 2d tensor.

    Note: eos/bos are supposed to be already added by sentencepiece
    """
    size = max(len(v) for v in values)
    size = max(size, pad_to_length)

    offset = 0
    if prepend_bos >= 0:
        assert not left_pad, "TODO: left_pad isn't compatible with prepend_bos."
        assert rewrite_bos < 0, "Can't use both rewrite_bos and prepend_bos."
        size += 1
        offset = 1
        rewrite_bos = prepend_bos

    if size % pad_to_multiple != 0:
        size = (size - size % pad_to_multiple) + pad_to_multiple

    batch_size = max(len(values), batch_size)
    res = torch.zeros((batch_size, size), dtype=dtype).fill_(pad_id)
    for i, v in enumerate(values):
        if left_pad:
            # TODO: make left_pad work with prepend_bos (who is using left_pad ?)
            res[i, size - len(v) :] = torch.tensor(v, dtype=dtype) + shift_tokens
        else:
            res[i, offset : len(v) + offset] = (
                torch.tensor(v, dtype=dtype) + shift_tokens
            )
    if rewrite_bos >= 0:
        assert not left_pad, "TODO: left_pad isn't compatible with rewrite_bos."
        res[:, 0] = rewrite_bos
    return res
