"""reports - plain-words market reports built only from candles and one validated bias. See CONTRACTS_AND_DESIGN.md."""
from .assemble import build, build_from_repo
from .source import bias_from_state, read_candles
