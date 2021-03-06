from . import constants, entropy
from ..data import utils as data_utils

import numpy as np
from scipy import stats
from sklearn.cluster import MeanShift, estimate_bandwidth
from operator import itemgetter
from collections import Counter, defaultdict

"""
Record and analyse windowed and non-windowed packet flow statistics.
"""

def ordered_tcp_payload_length_frequency(packets, tls_only=False, bandwidth=3):
    """
    Utilises meanshift to cluster input tcp frames by their payload to within
    a certain difference (bandwidth), and returns descending ordered clusters.
    This is useful if the PT sends a lot of unidirectional equal or similar
    length payloads, for which the packets should have been filtered by source or
    destination IP.

    :param list packets: a list of parsed packets, non-tcp packets will be ignored.
    :param bool tls_only: if True ignoring non-TLS frames, including TCP frames
        not containing TLS headers but segmented TLS data.
    :param int bandwidth: the maximum distance within clusters, i.e. max difference
        between payload lengths.
    :returns: a list of sets containing clustered values ordered from most
        frequent to least. Packets with TCP payload lengths close to a likely
        MTU limit are not included in clustering to prevent bias against commonly
        seen non-Large Segment Offload traces captured on devices.
    """

    # Collect the lengths.
    lengths = []
    for packet in packets:
        if packet['tcp_info'] is None:
            continue
        elif tls_only and packet['tls_info'] is None:
            continue
        elif len(packet['tcp_info']['payload']) > constants.MTU_FRAME_AVOIDANCE_THRESHOLD_CLUSTERING:
            continue
        else:
            lengths.append(len(packet['tcp_info']['payload']))

    # Cluster the lengths.
    lengths = np.array(list(zip(lengths, np.zeros(len(lengths)))), dtype=np.int)
    meanshift = MeanShift(bandwidth=bandwidth, bin_seeding=True)
    meanshift.fit(lengths)
    labels = meanshift.labels_
    labels_unique = np.unique(labels)
    n_clusters_ = len(labels_unique)

    # Return the top clusters in order.
    clusters = []
    for i in range(n_clusters_):
        members = (labels == i)
        clusters.append(set(lengths[members, 0]))

    return clusters


def ordered_udp_payload_length_frequency(packets, bandwidth=3):
    """
    Utilises meanshift to cluster input udp frames by their packet length to within
    a certain difference (bandwidth), and return descending ordered clusters.
    This is useful if the PT sends a lot of unidirectional equal or similar UDP
    length payloads, for which the packets should have been filtered by source or
    destination IP.

    :param list packets: a list of parsed packets, non-udp packets will be ignored.
    :param int bandwidth: the maximum distance within clusters, i.e. max difference
        between payload lengths.
    :returns: a list of sets containing clustered values ordered from the most
        frequent to the least.
    """

    # Collect the lengths.
    lengths = []
    for packet in packets:
        if packet['proto'] != "UDP":
            continue
        elif len(packet['len']) > constants.MTU_FRAME_AVOIDANCE_THRESHOLD_CLUSTERING:
            continue
        else:
            lengths.append(packet['len'])

    # Cluster the lengths.
    lengths = np.array(list(zip(lengths, np.zeros(len(lengths)))), dtype=np.int)
    meanshift = MeanShift(bandwidth=bandwidth, bin_seeding=True)
    meanshift.fit(lengths)
    labels = meanshift.labels_
    labels_unique = np.unique(labels)
    n_clusters_ = len(labels_unique)

    # Return the top clusters in order.
    clusters = []
    for i in range(n_clusters_):
        members = (labels == i)
        clusters.append(set(lengths[members, 0]))

    return clusters


def window_packets_fixed_size(packets, window_size):
    """
    Segment packets into fixed-size windows, discarding any remainder.

    :param list packets: a list of parsed packets.
    :param int window_size: the constant frame-count of each windowed segment,
        which will be segmented in chronological order.
    :returns: a 2-D list containing windowed packets.
    :raises: ValueError if the fixed window size is invalid.
    """

    if not isinstance(window_size, int) or window_size < 1:
        raise ValueError("Invalid window size.")

    if len(packets) < window_size:
        return [] # Empty list if insufficient size of input.

    segments = [packets[i:i+window_size] for i in range(0, len(packets), window_size)]

    if len(segments[-1]) != window_size:
        segments = segments[:-1]

    return segments


def window_packets_time_series(packets, chronological_window, sort=True):
    """
    Segment packets into fixed chronologically-sized windows.

    :param list packets: a list of parsed packets.
    :param int window_size: the number of **microseconds** elapsed covered by
        each windowed segment, in chronological order.
    :param bool sort: if True, packets will be sorted again into chronological order,
        useful if packet times not guaranteed to be chronologically ascending.
        True by default.
    :returns: a 2-D list containing windowed packets.
    """

    # Sorted by time if required.
    if sort:
        packets = sorted(packets, key=itemgetter('time'))

    # Convert to microseconds then integer timestamps, and move to zero
    # for performance.
    min_time = int(float(min(packets, key=itemgetter('time'))['time']) * 1000000)
    max_time = int(float(max(packets, key=itemgetter('time'))['time']) * 1000000)
    start_time = 0
    end_time = max_time - min_time
    if (max_time - min_time) < chronological_window:
        return [] # Empty list if packet duration too small.

    ts = [(t, t+chronological_window) for t in range(start_time, end_time, chronological_window)]
    segments = [[] for i in ts]
    c_segment = 0
    c_segment_max = len(ts) - 1

    for packet in packets:
        packet_t = float(packet['time']) * 1000000 - min_time # Same movement as done above.
        while (not ts[c_segment][0] <= packet_t < ts[c_segment][1]) and (c_segment < c_segment_max):
            c_segment += 1
        segments[c_segment].append(packet)

    return segments


def group_packets_by_ip_fixed_size(packets, clients, window_size):
    """
    Group packets into fixed-size segments that contain bidirectional traffic
    from and towards individual predefined clients, individual inputs should
    normally come from time-windowing by :func:`window_packets_time_series` (e.g. 60s)
    to simulate realistic firewall operation conditions.

    :param list packets: a list of parsed packets. Packets in this 1D list are
        assumed to be chronologically ordered.
    :param list clients: a predefined list of Python subnets objects describing
        clients that are considered within the firewall's control.
    :param int window_size: threshold to start a new segment.
    :returns: a dictionary indexed by a tuple of individual client and target pair,
        containing one 2D list for each pair. Each 2D list contains segmented
        packets by fixed size, with remainders retained.
    """

    if len(clients) < 1:
        return []

    client_packets = defaultdict(list)
    for packet in packets:

        packet_srcnet = data_utils.build_subnet(packet['src'])
        packet_dstnet = data_utils.build_subnet(packet['dst'])
        packet_client = None

        for client in clients:
            if client.overlaps(packet_srcnet):
                packet_client = packet_srcnet
                target = packet['dst']
                break

            elif client.overlaps(packet_dstnet):
                packet_client = packet_dstnet
                target = packet['src']
                break

        if not packet_client:
            continue # Irrelevant packet.

        # Append here first.
        client_packets[(str(packet_client), target)].append(packet)

    # Now segment for each client/target pair by fixed size.
    for c in client_packets:
        client_packets[c] = [client_packets[c][i:i+window_size] for i in range(0, len(client_packets[c]), window_size)]
        if len(client_packets[c][-1]) < window_size: # Discard remainder.
            client_packets[c] = client_packets[c][:-1]

    return client_packets


def get_window_stats(windowed_packets, client_ips, feature_selection=None):
    """
    Calculate the following features for the windowed packets:

    - 'max_entropy_up': max entropy of upstream TCP payloads;
    - 'min_entropy_up': min entropy of upstream TCP payloads;
    - 'mean_entropy_up': mean entropy of upstream TCP payloads;
    - 'mean_interval_up': upstream mean TCP ACK intervals;
    - 'bin_#_interval_up': the number of in-range intervals between TCP frames,
      ranges divided between 0, 1000, 10000, 100000, 1000000 microseconds,
      with value represented as the upper range of each interval. Only
      the first of all frames bearing the unique sequence number is
      counted;
    - 'top1_tcp_len_up': the most common upstream TCP payload length;
    - 'top2_tcp_len_up': the second most common upstream TCP payload length;
    - 'mean_tcp_len_up': mean upstream TCP payload length.
    - 'bin_#_len_up': binned TCP payload lengths in each direction of flow,
      divided between 100, 200, 500, 1000, 1500 bytes. 
    - 'push_ratio_up': ratio of TCP ACKs with PSH flags set, indicating
      reuse of TCP handshake for additional data;
    - (All attributes above, except for downstream and named '..._down');
    - 'up_down_ratio': ratio of upstream to downstream packets.

    Only relevant features will be calculated and returned, see below.

    :param list windowed_packets: a segment of TCP packets, **assumed to have
        been sorted by time in ascending order**.
    :param list client_ips: the IP addresses/subnets of the suspected PT clients.
    :param feature_selection: chooses sets of features to check for and
        include in the output. If None, include all features. Options:

        - :const:`~constants.USE_ENTROPY`       : Entropy features
        - :const:`~constants.USE_INTERVAL`      : Mean interval
        - :const:`~constants.USE_INTERVAL_BINS` : Binned intervals
        - :const:`~constants.USE_TCP_LEN`       : Top and mean TCP lengths
        - :const:`~constants.USE_TCP_LEN_BINS`  : Binned TCP lengths
        - :const:`~constants.USE_PSH`           : Ratio of PSH packets in ACK packets

    :returns: three-tuple: a dictionary containing the stats as described
        above, a set of remote IP addresses seen in the window, and a set of
        client ips seen in this window.
    """

    client_subnets = [data_utils.build_subnet(i) for i in client_ips]
    client_ips_seen = set([])

    all_features = True if (feature_selection is None) or (len(feature_selection) == 0) else False
    entropy_on = True if all_features else constants.USE_ENTROPY in feature_selection
    interval_on = True if all_features else constants.USE_INTERVAL in feature_selection
    interval_bins_on = True if all_features else constants.USE_INTERVAL_BINS in feature_selection
    tcp_len_on = True if all_features else constants.USE_TCP_LEN in feature_selection
    tcp_len_bins_on = True if all_features else constants.USE_TCP_LEN_BINS in feature_selection
    psh_on = True if all_features else constants.USE_PSH in feature_selection

    if not client_subnets:
        return {}, set([]), []

    stats = {}
    interval_ranges = [0, 1000, 10000, 100000, 1000000]
    tcp_len_ranges = [i*100 for i in range(0, 16)]
    max_tcp_len = max(tcp_len_ranges)
    # TCP length segmented into low/empty payload, moderate payloads, and
    # high/close-to-MTU payload. Most systems have a default MTU at nearly 1500
    # bytes.

    ips = set([])

    seqs_seen_up = set([])
    entropies_up = []
    intervals_up = []
    intervals_up_bins = {(interval_ranges[i-1], interval_ranges[i]): 0 for i in range(1, len(interval_ranges))}
    payload_lengths_up = []
    payload_lengths_up_bins = {(tcp_len_ranges[i-1], tcp_len_ranges[i]): 0 for i in range(1, len(tcp_len_ranges))}
    psh_up = 0
    ack_up = 0
    packets_up = list(filter(lambda x: any([i.overlaps(data_utils.build_subnet(x['src'])) for i in client_subnets]), windowed_packets))

    seqs_seen_down = set([])
    entropies_down = []
    intervals_down = []
    intervals_down_bins = {(interval_ranges[i-1], interval_ranges[i]): 0 for i in range(1, len(interval_ranges))}
    payload_lengths_down = []
    payload_lengths_down_bins = {(tcp_len_ranges[i-1], tcp_len_ranges[i]): 0 for i in range(1, len(tcp_len_ranges))}
    psh_down = 0
    ack_down = 0
    packets_down = list(filter(lambda x: any([i.overlaps(data_utils.build_subnet(x['dst'])) for i in client_subnets]), windowed_packets))

    if len(packets_up) > 0 and len(packets_down) > 0:
        stats['up_down_ratio'] = float(len(packets_up)) / len(packets_down)
    else:
        stats['up_down_ratio'] = 0

    # Now tally upstream frames.
    if len(packets_up) > 1:
        prev_time = None
        for packet in packets_up:

            # Ignore non-TCP packets.
            if packet['tcp_info'] == None:
                continue
            ips.add(packet['dst'])
            client_ips_seen.add(packet['src'])

            # Entropy tally.
            if entropy_on:
                packet_tcp = packet['tcp_info']
                entropies_up.append(entropy.EntropyAnalyser.byte_entropy(packet_tcp['payload']))

            # Interval information.
            if packet_tcp['seq'] not in seqs_seen_up:
                seqs_seen_up.add(packet_tcp['seq'])

                if prev_time is None:
                    prev_time = float(packet['time']) * 1000000
                else:
                    interval = abs(float(packet['time']) * 1000000 - prev_time) # Just in case not sorted, even though that would be incorrect.
                    intervals_up.append(interval)
                    # If the interval is above 1 second, ignore its bin membership.
                    for k in intervals_up_bins:
                        if k[0] <= interval < k[1]:
                            intervals_up_bins[k] += 1
                            break
                    prev_time = float(packet['time']) * 1000000

            # Payload length tally.
            up_len = len(packet_tcp['payload'])
            if tcp_len_on:
                payload_lengths_up.append(up_len)
            if tcp_len_bins_on:
                if up_len > max_tcp_len: # Deal with LSO.
                    payload_lengths_up_bins[(max_tcp_len-100, max_tcp_len)] += up_len // max_tcp_len
                    up_len = up_len % max_tcp_len
                for k in payload_lengths_up_bins:
                    if k[0] <= up_len < k[1]:
                        payload_lengths_up_bins[k] += 1
                        break

            # ACK/PSH information.
            if psh_on:
                if packet_tcp['flags']['ACK'] == True:
                    ack_up += 1
                    if packet_tcp['flags']['PSH'] == True:
                        psh_up += 1

        if entropy_on:
            stats['mean_entropy_up'] = np.mean(entropies_up)
            stats['max_entropy_up'] = np.max(entropies_up)
            stats['min_entropy_up'] = np.min(entropies_up)

        if interval_on:
            if len(intervals_up) == 0: # Cap at 1 second.
                stats['mean_interval_up'] = 1000000
            else:
                stats['mean_interval_up'] = np.mean(intervals_up)
        if interval_bins_on:
            intervals_up_bins = sorted(intervals_up_bins.items(), key=itemgetter(0))
            for interval_bin in intervals_up_bins:
                stats['bin_' + str(interval_bin[0]) + '_interval_up'] = float(interval_bin[1]) / len(packets_up)

        if tcp_len_on:
            up_counts = Counter(payload_lengths_up).items()
            up_counts_sorted = sorted(up_counts, key=itemgetter(1))
            stats['top1_tcp_len_up'] = up_counts_sorted[0][0] if len(up_counts_sorted) > 0 else 0
            stats['top2_tcp_len_up'] = up_counts_sorted[1][0] if len(up_counts_sorted) > 1 else 0
            stats['mean_tcp_len_up'] = np.mean(payload_lengths_up)
        if tcp_len_bins_on:
            payload_lengths_up_bins = sorted(payload_lengths_up_bins.items(), key=itemgetter(0))
            for length_bin in payload_lengths_up_bins:
                stats['bin_' + str(length_bin[0]) + '_len_up'] = float(length_bin[1]) / len(packets_up)

        if psh_on:
            if ack_up > 0:
                stats['push_ratio_up'] = float(psh_up) / ack_up
            else:
                stats['push_ratio_up'] = 0

    else: # Default to zeros if insufficient frames to check.

        if entropy_on:
            stats['mean_entropy_up'] = 0
            stats['max_entropy_up'] = 0
            stats['min_entropy_up'] = 0

        if interval_on:
            stats['mean_interval_up'] = 1000000
        if interval_bins_on:
            for interval_bin in intervals_up_bins:
                stats['bin_' + str(interval_bin) + '_interval_up'] = 0

        if tcp_len_on:
            stats['top1_tcp_len_up'] = 0
            stats['top2_tcp_len_up'] = 0
            stats['mean_tcp_len_up'] = 0
        if tcp_len_bins_on:
            for length_bin in payload_lengths_up_bins:
                stats['bin_' + str(length_bin) + '_len_up'] = 0

        if psh_on:
            stats['push_ratio_up'] = 0

    # Now tally downstream frames.
    if len(packets_down) > 0:
        prev_time = None
        for packet in packets_down:

            # Ignore non-TCP packets.
            if packet['tcp_info'] == None:
                continue
            ips.add(packet['src'])
            client_ips_seen.add(packet['dst'])

            # Entropy tally.
            if entropy_on:
                packet_tcp = packet['tcp_info']
                entropies_down.append(entropy.EntropyAnalyser.byte_entropy(packet_tcp['payload']))

            # Interval information.
            if packet_tcp['seq'] not in seqs_seen_down:
                seqs_seen_down.add(packet_tcp['seq'])

                if prev_time is None:
                    prev_time = float(packet['time']) * 1000000
                else:
                    interval = abs(float(packet['time']) * 1000000 - prev_time)
                    intervals_down.append(interval)
                    # If the interval is above 1 second, ignore its bin membership.
                    for k in intervals_down_bins:
                        if k[0] <= interval < k[1]:
                            intervals_down_bins[k] += 1
                            break
                    prev_time = float(packet['time']) * 1000000


            # Payload length tally.
            down_len = len(packet_tcp['payload'])
            if tcp_len_on:
                payload_lengths_down.append(down_len)
            if tcp_len_bins_on:
                if down_len > max_tcp_len: # Deal with LSO.
                    payload_lengths_down_bins[(max_tcp_len-100, max_tcp_len)] += down_len // max_tcp_len
                    down_len = down_len % max_tcp_len
                for k in payload_lengths_down_bins:
                    if k[0] <= down_len < k[1]:
                        payload_lengths_down_bins[k] += 1
                        break
                    

            # ACK/PSH information.
            if psh_on:
                if packet_tcp['flags']['ACK'] == True:
                    ack_down += 1
                    if packet_tcp['flags']['PSH'] == True:
                        psh_down += 1

        if entropy_on:
            stats['mean_entropy_down'] = np.mean(entropies_down)
            stats['max_entropy_down'] = np.max(entropies_down)
            stats['min_entropy_down'] = np.min(entropies_down)

        if interval_on:
            if len(intervals_down) == 0: # Cap at 1 second.
                stats['mean_interval_down'] = 1000000
            else:
                stats['mean_interval_down'] = np.mean(intervals_down)
        if interval_bins_on:
            intervals_down_bins = sorted(intervals_down_bins.items(), key=itemgetter(0))
            for interval_bin in intervals_down_bins:
                stats['bin_' + str(interval_bin[0]) + '_interval_down'] = float(interval_bin[1]) / len(packets_down)

        if tcp_len_on:
            down_counts = Counter(payload_lengths_down).items()
            down_counts_sorted = sorted(down_counts, key=itemgetter(1))
            stats['top1_tcp_len_down'] = down_counts_sorted[0][0] if len(down_counts_sorted) > 0 else 0
            stats['top2_tcp_len_down'] = down_counts_sorted[1][0] if len(down_counts_sorted) > 1 else 0
            stats['mean_tcp_len_down'] = np.mean(payload_lengths_down)
        if tcp_len_bins_on:
            payload_lengths_down_bins = sorted(payload_lengths_down_bins.items(), key=itemgetter(0))
            for length_bin in payload_lengths_down_bins:
                stats['bin_' + str(length_bin[0]) + '_len_down'] = float(length_bin[1]) / len(packets_down)

        if psh_on:
            if ack_down > 0:
                stats['push_ratio_down'] = float(psh_down) / ack_down
            else:
                stats['push_ratio_down'] = 0

    else:
        # Default to least optimistic if insufficient frames to check.
        if entropy_on:
            stats['mean_entropy_down'] = 0
            stats['max_entropy_down'] = 0
            stats['min_entropy_down'] = 0

        if interval_on:
            stats['mean_interval_down'] = 1000000
        if interval_bins_on:
            for interval_bin in intervals_down_bins:
                stats['bin_' + str(interval_bin) + '_interval_down'] = 0

        if tcp_len_on:
            stats['top1_tcp_len_down'] = 0
            stats['top2_tcp_len_down'] = 0
            stats['mean_tcp_len_down'] = 0
        if tcp_len_bins_on:
            for length_bin in payload_lengths_down_bins:
                stats['bin_' + str(length_bin) + '_len_down'] = 0

        if psh_on:
            stats['push_ratio_down'] = 0


    return stats, ips, client_ips_seen


def synchronise_packets(packets, target_time, sort=False):
    """
    Synchronise the input packets with another trace by shifting the time of
    the first frame to align with the target time supplied.

    :param list packets: input packets to be time shifted, should be chronologically
        ordered or have sort set to True, otherwise results will be erroneous.
    :param float target_time: a valid UNIX timestamp to at least 6 d.p.
    :param bool sort: if True, the function will chronologically sort the input
        packets first.
    :returns: time shifted input packets.
    """

    if not isinstance(target_time, float):
        raise ValueError("Invalid target time.")

    if len(packets) == 0:
        return []

    if sort:
        packets = sorted(packets, key=itemgetter('time'))

    packets_start_time = float(packets[0]['time'])
    diff = target_time - packets_start_time

    for packet in packets:
        packet['time'] = float(packet['time']) + diff

    return packets
