import analytics, data
from strategy.strategy import DetectionStrategy

import os
from sys import exit, argv
from datetime import date, datetime
from operator import itemgetter
from math import log1p

class LengthClusteringStrategy(DetectionStrategy):
    """
    Detecting polling-based PTs such as meek by clustering the payload length
    of TLS-loaded TCP packet, useful for PTs with frequent directional pings
    with small and not greatly varying lengths of payloads.
    """

    NAME = "Length Clustering Strategy"
    DESCRIPTION = "Detecting low-payload heartbeat messages. TLS modes: only (TLS packets only), all (all packets), none (non-TLS packets only)."
    _DEBUG_PREFIX = "LenClustering"
    RUN_CONFIG_DESCRIPTION = ["MeanShift bandwidth"]

    TLS_INCLUSION_THRESHOLD = 0.1
    MEANSHIFT_BWS = [1, 2, 3, 5, 10]
    MINIMUM_TPR = 0.40
    # While this method does not require high TPR, a minimum threshold needs to
    # be maintained to ensure fitness.

    TLS_MODES = ["all", "only", "none"]
    # Decide whether to use all traces, only TLS traces, or only non-TLS traces.


    def __init__(self, pt_pcap, negative_pcap=None, debug=True):
        super().__init__(pt_pcap, negative_pcap, debug=debug)
        self._strategic_states['TPR'] = {}
        self._strategic_states['FPR'] = {}
        self._strategic_states['top_cluster'] = {}
        self._strategic_states['top_two_clusters'] = {}
        self._strategic_states['blocked'] = {}
        self._tls_mode = self.TLS_MODES[0]


    def set_strategic_filter(self):
        """
        When detecting meek, it would be trivial to simply ignore all non-TLS
        packets. However for a generalised strategy use/disregard of TLS packets
        should be determined by inspecting the positive traces instead. Therefore
        it is only necessary to filter out TCP packets with no payload.
        """

        self._strategic_packet_filter = {"tcp_info": {"$ne": None},
         "tcp_info.payload": {"$ne": b''}}


    def interpret_config(self, config_set):
        """
        Bandwidth is used to distinguish length clustering runs.
        """
        if config_set is not None:
            return "TCP payload length clustering at MeanShift bandwidth {}.".format(config_set[0])
        else:
            return ""


    def config_specific_penalisation(self, config_set):
        """
        The smaller the cluster bandwidth, the easier it is to perform live
        TCP payload length-based interceptions. Therefore 5% of penalty for
        every 1 byte of cluster bandwidth beyond the minimum.
        """

        if isinstance(config_set, tuple) and isinstance(config_set[0], int) and \
         min(self.MEANSHIFT_BWS) <= config_set[0] <= max(self.MEANSHIFT_BWS):
            return 0.05 * (config_set[0] - min(self.MEANSHIFT_BWS))


    def test_validation_split(self, split_ratio):
        """
        Not currently needed, as a fixed strategy is used.
        """

        return ([], [])


    def positive_run(self, **kwargs):
        """
        Because this simple strategy is based on common global TCP payload lengths,
        the identified trace ratio is not very useful here.
        :param bandwidth: the bandwidth used for meanshift clustering payload lengths.
        """

        bandwidth = 1 if 'bandwidth' not in kwargs else kwargs['bandwidth']

        if self._tls_mode == "only":
            most_frequent = analytics.traffic.ordered_tcp_payload_length_frequency(self._pt_traces, True, bandwidth)
        else:
            most_frequent = analytics.traffic.ordered_tcp_payload_length_frequency(self._pt_traces, False, bandwidth)
        top_cluster = most_frequent[0]
        top_two_clusters = top_cluster.union(most_frequent[1])
        top_cluster_identified = 0
        top_two_clusters_identified = 0
        for trace in self._pt_traces:
            if len(trace['tcp_info']['payload']) in top_cluster:
                top_cluster_identified += 1
                if len(trace['tcp_info']['payload']) in top_two_clusters:
                    top_two_clusters_identified += 1

        # Pass the cluster to the negative run.
        self._strategic_states['top_cluster'][bandwidth] = top_cluster
        self._strategic_states['top_two_clusters'][bandwidth] = top_two_clusters

        self._strategic_states['TPR'][(bandwidth, 1)] = top_cluster_identified / len(self._pt_traces)
        self._strategic_states['TPR'][(bandwidth, 2)] = top_two_clusters_identified / len(self._pt_traces)
        self.debug_print("TCP payload lengths in the top cluster: {}.".format(', '.join([str(i) for i in list(top_cluster)])))
        self.debug_print("TCP payload lengths in top clusters: {}.".format(', '.join([str(i) for i in list(top_two_clusters)])))

        return max(self._strategic_states['TPR'][(bandwidth, 1)], self._strategic_states['TPR'][(bandwidth, 2)])


    def negative_run(self, **kwargs):
        """
        Now we check the identified lengths against negative traces. Because
        TLS packets with a TCP payload as small as meek's are actually very
        rare, this simple strategy becomes effective.
        :param bandwidth: the bandwidth used for meanshift clustering payload lengths.
        """

        bandwidth = 1 if 'bandwidth' not in kwargs else kwargs['bandwidth']

        top_cluster = self._strategic_states['top_cluster'][bandwidth]
        top_falsely_identified = 0
        self._strategic_states['blocked'][(bandwidth, 1)] = set([])
        for trace in self._neg_traces:
            if len(trace['tcp_info']['payload']) in top_cluster:
                top_falsely_identified += 1
                self._strategic_states['blocked'][(bandwidth, 1)].add(trace['dst'])
        blocked_one = self._strategic_states['blocked'][(bandwidth, 1)]

        top_two_clusters = self._strategic_states['top_two_clusters'][bandwidth]
        top_two_falsely_identified = 0
        self._strategic_states['blocked'][(bandwidth, 2)] = set([])
        for trace in self._neg_traces:
            if len(trace['tcp_info']['payload']) in top_two_clusters:
                top_two_falsely_identified += 1
                self._strategic_states['blocked'][(bandwidth, 2)].add(trace['dst'])
        blocked_two = self._strategic_states['blocked'][(bandwidth, 2)]

        # Unlike the positive case, we consider the false positive rate to be
        # over all traces, rather than just the ones were are interested in.
        self._strategic_states['FPR'][(bandwidth, 1)] = float(top_falsely_identified) / self._neg_collection_total
        self._strategic_states['FPR'][(bandwidth, 2)] = float(top_two_falsely_identified) / self._neg_collection_total
        self._negative_blocked_ips = min([blocked_one, blocked_two], key=len)
        self._false_positive_blocked_rate = float(len(self._negative_blocked_ips)) / self._negative_unique_ips

        return min(self._strategic_states['FPR'][(bandwidth, 1)], self._strategic_states['FPR'][(bandwidth, 2)])


    def report_blocked_ips(self):
        """
        Return a Wireshark-compatible filter expression to allow viewing blocked
        traces in Wireshark. Useful for studying false positives.
        :returns: a Wireshark-compatible filter expression string.
        """

        if self._tls_mode == "all":
            wireshark_output = "tcp.payload && ("
        elif self._tls_mode == "only":
            wireshark_output = "ssl & tcp.payload && ("
        elif self._tls_mode == "none":
            wireshark_output = "!ssl & tcp.payload && ("
        for i, ip in enumerate(list(self._negative_blocked_ips)):
            wireshark_output += "ip.dst_host == \"" + ip + "\" "
            if i < len(self._negative_blocked_ips) - 1:
                wireshark_output += "|| "
        wireshark_output += ") && ("
        for i, l in enumerate(list(self._strategic_states['top_cluster'])):
            wireshark_output += "tcp.len == " + str(l)
            if i < len(self._strategic_states['top_cluster']) - 1:
                wireshark_output += " || "
        wireshark_output += ")"

        return wireshark_output


    def run_strategy(self, **kwargs):
        """
        PT clients and servers in the input PCAP should be specified via IP_SRC
        and IP_DST respectively, while negative clients should be specified via
        IP_SRC.
        :param tls_mode: Optionally set tls_mode between "all", "only", or "none"
            to test all packets, TLS packets only, or non-TLS packets only. Set
            it as "guess" or omit this parameter for the strategy to guess.
        """

        # Check whether we should include or disregard TLS packets.
        tls_mode = 'guess' if 'tls_mode' not in kwargs else kwargs['tls_mode']
        if tls_mode not in self.TLS_MODES: # Specified but invalid.
            tls_mode = 'guess'

        if tls_mode == 'guess':
            self.debug_print("Studying PT traces to figure out about TLS packets")
            tls_traces = 0
            for t in self._pt_traces:
                if 'tls_info' in t and t['tls_info'] is not None:
                    tls_traces += 1
            if float(tls_traces) / len(self._pt_traces) > 0.95:
                self._tls_mode = "only"
            elif float(tls_traces) / len(self._pt_traces) < 0.05:
                self._tls_mode = "none"
            else:
                self._tls_mode = "all"
        else:
            self._tls_mode = tls_mode

        if tls_mode == 'only':
            self.debug_print("Strategy TLS mode: examining TLS packets only.")
            self._pt_traces = [i for i in self._pt_traces if i["tls_info"] is not None]
            self._neg_traces = [i for i in self._neg_traces if i["tls_info"] is not None]
        elif tls_mode == 'none':
            self.debug_print("Strategy TLS mode: examining non-TLS packets only.")
            self._pt_traces = [i for i in self._pt_traces if i["tls_info"] is None]
            self._neg_traces = [i for i in self._neg_traces if i["tls_info"] is None]
        else:
            self.debug_print("Strategy TLS mode: examining all packets regardless of TLS status.")

        self.debug_print("- Testing the following bandwidths for MeanShift: {}".format(', '.join([str(i) for i in self.MEANSHIFT_BWS])))
        for bw in self.MEANSHIFT_BWS:

            self.debug_print("- Running MeanShift on positives with bandwidth {}...".format(bw))
            self.run_on_positive((bw,), bandwidth=bw)
            tpr_top_cluster = self._strategic_states['TPR'][(bw, 1)]
            tpr_top_two_clusters = self._strategic_states['TPR'][(bw, 2)]
            self.debug_print("True positive rate on bandwidth {} for top cluster: {}".format(bw, tpr_top_cluster))
            self.debug_print("True positive rate on bandwidth {} for top two clusters: {}".format(bw, tpr_top_two_clusters))

            self.debug_print("- Checking MeanShift on negatives with bandwidth {}...".format(bw))
            self.run_on_negative((bw,), bandwidth=bw)
            fpr_top_cluster = self._strategic_states['FPR'][(bw, 1)]
            fpr_top_two_clusters = self._strategic_states['FPR'][(bw, 2)]
            self.debug_print("False positive rate on bandwidth {} for top cluster: {}".format(bw, fpr_top_cluster))
            self.debug_print("False positive rate on bandwidth {} for top two clusters: {}".format(bw, fpr_top_two_clusters))

        # Round performance to four decimal places.
        tps = self._strategic_states['TPR']
        fps = self._strategic_states['FPR']

        # Find the best true positive and false positive performance.
        # Descending order of TPR, then ascending by bandwidth and cluster size to maximise efficiency.
        best_true_positives = [i[0] for i in sorted(tps.items(), key=lambda x: (x[1], -x[0][0], -x[0][1]), reverse=True)]
        # False positive in ascending order, then by bandwidth and cluster size ascending.
        best_false_positives = [i[0] for i in sorted(fps.items(), key=lambda x: (x[1], x[0][0], x[0][1]))]

        # Walk down the list of lowest false positives to find the first config
        # satisfying the minimum true positive rate requirement.
        best_config = None
        for config in best_false_positives:
            if tps[config] >= self.MINIMUM_TPR:
                best_config = config
                break

        # If none satisfies the minimum true positive rate requirement, report
        # as failure.
        if best_config is None:
            self.debug_print("No bandwidth and cluster size achieved the minimum true positive rate required ({}), giving up.".format(self.MINIMUM_TPR))
            return (None, None)

        self._true_positive_rate = tps[best_config]
        self._false_positive_rate = fps[best_config]
        if best_config[1] == 1:
            self._strategic_states['top_cluster'] = self._strategic_states['top_cluster'][best_config[0]]
        else:
            self._strategic_states['top_cluster'] = self._strategic_states['top_two_clusters'][best_config[0]]

        self.debug_print("Best classification performance:")
        self.debug_print("Bandwidth: {}, using top {} cluster(s).".format(best_config[0], best_config[1]))
        self.debug_print("True positive rate: {}; False positive rate: {}".format(self._true_positive_rate, self._false_positive_rate))

        self._negative_blocked_ips = self._strategic_states['blocked'][best_config]
        self._false_positive_blocked_rate = float(len(self._negative_blocked_ips)) / self._negative_unique_ips
        self.debug_print("This classification configuration blocked {:0.2f}% of IPs seen.".format(self._false_positive_blocked_rate))

        return (self._true_positive_rate, self._false_positive_rate)


if __name__ == "__main__":
    parent_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    pt_path = os.path.join(parent_path, 'examples', 'local', argv[1])
    unobfuscated_path = os.path.join(parent_path, 'examples', 'local', argv[2])
    detector = LengthClusteringStrategy(pt_path, unobfuscated_path, debug=True)
    detector.setup(pt_ip_filters=[(argv[3], data.constants.IP_SRC),
     (argv[4], data.constants.IP_DST)], negative_ip_filters=[(argv[5],
     data.constants.IP_SRC)], pt_collection=argv[6], negative_collection=argv[7])
    detector.run(tls_mode=argv[8])

    print(detector.report_blocked_ips())
    score, best_config = detector._score_performance_stats()
    print("Score: {}, best config: {}.".format(score, detector.interpret_config(best_config)))
    print(detector.make_csv())
