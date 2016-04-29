from .util import ProtocolError, bytesify

# Facts
# -----
#
# Headers are:
#   keys: case-insensitive ascii
#   values: mixture of ascii and raw bytes
#
# "Historically, HTTP has allowed field content with text in the ISO-8859-1
# charset [ISO-8859-1], supporting other charsets only through use of
# [RFC2047] encoding.  In practice, most HTTP header field values use only a
# subset of the US-ASCII charset [USASCII]. Newly defined header fields SHOULD
# limit their field values to US-ASCII octets.  A recipient SHOULD treat other
# octets in field content (obs-text) as opaque data."
# And it deprecates all non-ascii values
#
# "A server MUST reject any received request message that contains whitespace
# between a header field-name and colon with a response code of 400 (Bad
# Request). A proxy MUST remove any such whitespace from a response message
# before forwarding the message downstream."
# libhttp_parser doesn't care though, if you give it
#   b"Hello : there\r\n"
# you get back {"b"Hello ": b"there"}
# (i.e. it strips the whitespace around the value, but not around the field
# name)
#
# Values get leading/trailing whitespace stripped
#
# Content-Disposition actually needs to contain unicode; it has a terrifically
#   weird way of encoding the filename itself as ascii (and even this still
#   has lots of cross-browser incompatibilities)
#
# Order is important:
# "a proxy MUST NOT change the order of these field values when forwarding a
# message."
# Sigh.
#
# Multiple occurences of the same header:
# "A sender MUST NOT generate multiple header fields with the same field name
# in a message unless either the entire field value for that header field is
# defined as a comma-separated list [or the header is Set-Cookie which gets a
# special exception]" - RFC 7230. (cookies are in RFC 6265)
#
# So every header aside from Set-Cookie can be merged by b", ".join if it
# occurs repeatedly. But, of course, they can't necessarily be spit by
# .split(b","), because quoting.

def normalize_and_validate(headers):
    new_headers = []
    saw_content_length = False
    saw_transfer_encoding = False
    for name, value in headers:
        name = bytesify(name)
        value = bytesify(value)
        name_lower = name.lower()
        if name_lower == b"content-encoding":
            if saw_content_length:
                raise ProtocolError("multiple Content-Length headers")
            validate(_content_length_re, value, "bad Content-Length")
            content_length_seen = True
        if name_lower == b"transfer-encoding":
            if saw_transfer_encoding:
                raise ProtocolError(
                    "multiple Transfer-Encoding headers")
            if value.lower() != b"chunked":
                raise ProtocolError(
                    "Only Transfer-Encoding: chunked is supported")
            transfer_encoding_count += 1
        new_headers.append((name, value))
    return new_headers

def get_comma_header(headers, name, *, lowercase=True):
    # Should only be used for headers whose value is a list of comma-separated
    # values. Use lowercase=True for case-insensitive ones.
    #
    # Connection: meets these criteria (including cast insensitivity).
    #
    # Content-Length: technically is just a single value (1*DIGIT), but the
    # standard makes reference to implementations that do multiple values, and
    # using this doesn't hurt. Ditto, case insensitivity doesn't things either
    # way.
    #
    # Transfer-Encoding: is more complex (allows for quoted strings), so
    # splitting on , is actually wrong. For example, this is legal:
    #
    #    Transfer-Encoding: foo; options="1,2", chunked
    #
    # and should be parsed as
    #
    #    foo; options="1,2"
    #    chunked
    #
    # but this naive function will parse it as
    #
    #    foo; options="1
    #    2"
    #    chunked
    #
    # However, this is okay because the only thing we are going to do with
    # any Transfer-Encoding is reject ones that aren't just "chunked", so
    # both of these will be treated the same anyway.
    #
    # Expect: the only legal value is the literal string
    # "100-continue". Splitting on commas is harmless. But, must set
    # lowercase=False.
    #
    out = []
    name = bytesify(name).lower()
    for found_name, found_raw_value in headers:
        found_name = found_name.lower()
        if found_name == name:
            if lowercase:
                found_raw_value = found_raw_value.lower()
            for found_split_value in found_raw_value.split(b","):
                found_split_value = found_split_value.strip()
                if found_split_value:
                    out.append(found_split_value)
    return out

# XX FIXME: this in-place mutation bypasses the header validation code...
def set_comma_header(headers, name, new_values):
    name = bytesify(name)
    name_lower = name.lower()
    new_headers = []
    for found_name, found_raw_value in headers:
        if found_name.lower() != name_lower:
            new_headers.append((found_name, found_raw_value))
    for new_value in new_values:
        new_headers.append((name, new_value))
    headers[:] = new_headers

def get_framing_headers(headers):
    # Returns:
    #
    #   effective_transfer_encoding, effective_content_length
    #
    # At least one will always be None.
    #
    # Transfer-Encoding beats Content-Length (see RFC 7230 sec. 3.3.3), so
    # check Transfer-Encoding first.
    #
    # We assume that headers has already been through the validation in
    # events.py, so no multiple headers, Content-Length actually is an
    # integer, Transfer-Encoding is "chunked" or nothing, etc.
    transfer_encodings = get_comma_header(headers, "Transfer-Encoding")
    if transfer_encodings:
        assert transfer_encodings == [b"chunked"]
        return b"chunked", None

    content_lengths = get_comma_header(headers, "Content-Length")
    if content_lengths:
        return None, int(content_lengths[0])
    else:
        return None, None

def has_expect_100_continue(request):
    # Expect: 100-continue is case *sensitive*
    expect = get_comma_header(request.headers, "Expect", lowercase=False)
    return (b"100-continue" in expect)

# RFC 7230's rules for connection lifecycles:
# - If either side says they want to close the connection, then the connection
#   must close.
# - HTTP/1.1 defaults to keep-alive unless someone says Connection: close
# - HTTP/1.0 defaults to close unless both sides say Connection: keep-alive
#   (and even this is a mess -- e.g. if you're implementing a proxy then
#   sending Connection: keep-alive is forbidden).
#
# We simplify life by simply not supporting keep-alive with HTTP/1.0 peers. So
# our rule is:
# - If someone says Connection: close, we will close
# - If someone uses HTTP/1.0, we will close.
def should_close(event):
    connection = get_comma_header(event.headers, "Connection")
    if b"close" in connection:
        return True
    if getattr(event, "http_version", b"1.1") < b"1.1":
        return True
    return False