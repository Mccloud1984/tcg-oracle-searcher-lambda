"""IntArray: a list of ints the SQL layer binds as an integer array (colour-mask subsets), not as JSON."""


class IntArray(list):
    """A list that the SQL layer binds as a native integer array, not JSON."""
