from typing import Union
import numpy as np


class CharacterTokenizer:
    """Facilitates Conversion between vocab <=> integers (tokenization)

    Args:
        tokens: (iterable) previously extracted tokens (i.e. list of characters or words)
    """
    def __init__(self, tokens: Union[list, tuple] = None):

        # Character to index and index to character maps
        # import pdb; pdb.set_trace()
        self._tokens = list(set(tokens)) if tokens is not None else list()
        self.char_to_ix_dct = {ch: i for i, ch in enumerate(self._tokens)}
        self.ix_to_char_dct = {i: ch for i, ch in enumerate(self._tokens)}

    @property
    def vocab_size(self):
        return len(self.char_to_ix_dct)

    def add_token(self, token):
        if token not in self.char_to_ix_dct:
            self._tokens.append(token)
            self.char_to_ix_dct[token] = len(self.char_to_ix_dct)
            self.ix_to_char_dct[len(self.ix_to_char_dct)] = token
        return self.char_to_ix_dct[token]

    @property
    def tokens(self):
        return self._tokens

    def char_to_ix(self, char: str = None):
        return self.char_to_ix_dct[char]

    def ix_to_char(self, ix: Union[int, np.ndarray] = None):

        if isinstance(ix, np.ndarray):
            def f(x):
                return self.ix_to_char_dct[x]
            vf = np.vectorize(f)
            return vf(ix)

        elif any([isinstance(ix, d) for d in [int, np.int32, np.int64]]):
            return self.ix_to_char_dct[ix]
        else:
            raise ValueError("Tokenizer expected either int or array as input but got {}".format(type(ix)))

    def tokenize(self, sentence: str = None) -> np.ndarray:
        return np.array(list(map(self.char_to_ix, sentence)), dtype=np.int32)
