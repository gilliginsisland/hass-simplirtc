"""Experimental SDP-only offer/offer bridge for the browser harness.

This does not terminate ICE, DTLS, SCTP, or RTP. Media payload identifiers must
already agree; diagnostics identify mismatches instead of silently rewriting RTP.
"""


def split_sdp(sdp):
    parts = sdp.replace('\r\n', '\n').strip().split('\nm=')
    return parts[0].splitlines(), [('m='+part).splitlines() for part in parts[1:]]


def mid(section):
    return next(line[6:] for line in section if line.startswith('a=mid:'))


def codec_map(section):
    return {line.split()[0][9:]:line.split()[1].lower() for line in section if line.startswith('a=rtpmap:')}


def synthesize_answer(offer_sdp, other_offer_sdp, *, setup, report=lambda _:None):
    """Answer offer_sdp using the other offerer's transport and media parameters."""
    header, offered = split_sdp(offer_sdp)
    other_header, other = split_sdp(other_offer_sdp)
    by_kind = {section[0].split()[0][2:]:section for section in other}
    transport_prefixes=('a=ice-ufrag:', 'a=ice-pwd:', 'a=fingerprint:')
    base_transport=[line for line in other_header+other[0] if line.startswith(transport_prefixes)]
    result=[line for line in other_header if not line.startswith(('a=group:', 'a=ice-', 'a=fingerprint:', 'a=setup:'))]
    result.append('a=group:BUNDLE '+' '.join(mid(section) for section in offered))
    if 'a=ice-lite' in other_header:result.append('a=ice-lite')
    for target in offered:
        kind=target[0].split()[0][2:]
        source=by_kind.get(kind)
        source_lines=(source or [])
        transport=list({line.split(':',1)[0]:line for line in base_transport+other_header+source_lines if line.startswith(transport_prefixes)}.values())
        target_codecs=codec_map(target)
        source_codecs=codec_map(source_lines)
        if kind!='application' and source:
            common={pt for pt,name in target_codecs.items() if source_codecs.get(pt)==name}
            report(f'{kind} identical payload mappings: {len(common)}')
            if not common:raise ValueError(f'No identical {kind} payload mappings')
            formats=[pt for pt in target[0].split()[3:] if pt in common]
        else:
            formats=target[0].split()[3:]
        result.append(' '.join(target[0].split()[:3]+formats))
        result.append('c=IN IP4 0.0.0.0')
        result.extend(dict.fromkeys(transport))
        result.extend(['a=setup:'+setup, 'a=mid:'+mid(target)])
        if kind=='application':
            result.extend(line for line in (source or target) if line.startswith(('a=sctp-port:', 'a=max-message-size:')))
            continue
        result.append('a=sendonly' if 'a=recvonly' in target else 'a=recvonly')
        result.append('a=rtcp-mux')
        # Keep target's negotiated payload numbering. Source describes the sender.
        for line in target:
            if line.startswith(('a=rtpmap:', 'a=fmtp:', 'a=rtcp-fb:')):
                pt=line.split()[0].split(':',1)[1]
                if pt in formats or pt=='*':result.append(line)
        for line in source_lines:
            if line.startswith(('a=extmap:', 'a=ssrc:', 'a=ssrc-group:', 'a=msid:')):result.append(line)
        if source is None:
            result.extend(line for line in target if line.startswith('a=extmap:'))
    return '\r\n'.join(result)+'\r\n'
