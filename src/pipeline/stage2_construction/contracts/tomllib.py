from _tomllib_parser import TOMLDecodeError, load, loads
TOMLDecodeError.__module__ = __name__
__all__ = ("loads", "load", "TOMLDecodeError")
