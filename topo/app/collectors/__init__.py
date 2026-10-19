def base_url(value):
    """'10.0.0.100' -> 'http://10.0.0.100'. A full URL (scheme, port) is kept as typed."""
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    return value
