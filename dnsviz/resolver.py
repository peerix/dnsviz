#
# This file is a part of DNSViz, a tool suite for DNS/DNSSEC monitoring,
# analysis, and visualization.
# Created by Casey Deccio (casey@deccio.net)
#
# Copyright 2014-2015 VeriSign, Inc.
# 
# DNSViz is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# DNSViz is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import random
import time

import query
from ipaddr import IPAddr

import dns.rdataclass, dns.exception, dns.rcode, dns.resolver

MAX_CNAME_REDIRECTION = 20

_r = None
def get_standard_resolver():
    global _r
    if _r is None:
        _r = Resolver.from_file('/etc/resolv.conf', query.StandardRecursiveQuery)
    return _r

_rd = None
def get_dnssec_resolver():
    global _rd
    if _rd is None:
        _rd = Resolver.from_file('/etc/resolv.conf', query.RecursiveDNSSECQuery)
    return _rd

class DNSAnswer:
    '''An answer to a DNS query, including the full DNS response message, the
    RRset requested, and the server.'''

    def __init__(self, qname, rdtype, response, server):
        self.response = response
        self.server = server

        self.rrset = None

        self._handle_nxdomain(response)

        i = 0
        qname_sought = qname
        while i < MAX_CNAME_REDIRECTION:
            try:
                self.rrset = response.find_rrset(response.answer, qname_sought, dns.rdataclass.IN, rdtype)
                i = MAX_CNAME_REDIRECTION
            except KeyError:
                try:
                    rrset = response.find_rrset(response.answer, qname_sought, dns.rdataclass.IN, dns.rdatatype.CNAME)
                    qname_sought = rrset[0].target
                except KeyError:
                    break
            i += 1

        self._handle_noanswer()

    def _handle_nxdomain(self, response):
        if response.rcode() == dns.rcode.NXDOMAIN:
            raise dns.resolver.NXDOMAIN()

    def _handle_noanswer(self):
        if self.rrset is None:
            raise dns.resolver.NoAnswer()

class DNSAnswerNoAnswerAllowed(DNSAnswer):
    '''An answer to a DNS query, including the full DNS response message, the
    RRset requested, and the server.'''

    def _handle_noanswer(self):
        pass

class Resolver:
    '''A simple stub DNS resolver.'''

    def __init__(self, servers, query_cls, timeout=1.0, max_attempts=5, lifetime=15.0, shuffle=False):
        if lifetime is None and max_attempts is None:
            raise ValueError("At least one of lifetime or max_attempts must be specified for a Resolver instance.")

        self._servers = servers
        self._query_cls = query_cls
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._lifetime = lifetime
        self._shuffle = shuffle

    @classmethod
    def from_file(cls, resolv_conf, query_cls):
        servers = []
        try:
            with open(resolv_conf, 'r') as f:
                for line in f:
                    line = line.strip()
                    words = line.split()
                    if len(words) > 1 and words[0] == 'nameserver':
                        try:
                            servers.append(IPAddr(words[1]))
                        except ValueError:
                            pass
        except IOError:
            pass
        if not servers:
            servers.append(IPAddr('127.0.0.1'))
        return Resolver(servers, query_cls)

    def query(self, qname, rdtype, rdclass=dns.rdataclass.IN, allow_noanswer=False):
        answer = self.query_multiple((qname, rdtype, rdclass), allow_noanswer=allow_noanswer).values()[0]
        if isinstance(answer, DNSAnswer):
            return answer
        else:
            raise answer

    def query_multiple(self, *query_tuples, **kwargs):
        valid_servers = {}
        answers = {}
        attempts = {}

        if kwargs.get('allow_noanswer', False):
            answer_cls = DNSAnswerNoAnswerAllowed
        else:
            answer_cls = DNSAnswer

        query_tuples = set(query_tuples)
        for query_tuple in query_tuples:
            attempts[query_tuple] = 0
            valid_servers[query_tuple] = set(self._servers)

        if self._shuffle:
            servers = self._servers[:]
            random.shuffle(servers)
        else:
            servers = self._servers

        tuples_to_query = query_tuples.difference(answers)
        start = time.time()
        while tuples_to_query and (self._lifetime is None or time.time() - start < self._lifetime):
            now = time.time()
            queries = {}
            for query_tuple in tuples_to_query:
                if not valid_servers[query_tuple]:
                    answers[query_tuple] = dns.resolver.NoNameservers()
                    continue

                while query_tuple not in queries:
                    cycle_num, server_index = divmod(attempts[query_tuple], len(servers))
                    # if we've exceeded our maximum attempts, then break out
                    if cycle_num >= self._max_attempts:
                        answers[query_tuple] = dns.exception.Timeout()
                        break

                    server = servers[server_index]
                    if server in valid_servers[query_tuple]:
                        timeout = min(self._timeout, max((start + self._lifetime) - now, 0))
                        q = self._query_cls(query_tuple[0], query_tuple[1], query_tuple[2], server, None, query_timeout=timeout, max_attempts=1)
                        queries[query_tuple] = q

                    attempts[query_tuple] += 1

            query.ExecutableDNSQuery.execute_queries(*queries.values())

            for query_tuple, q in queries.items():
                server, client_response = q.responses.items()[0]
                client, response = client_response.items()[0]
                if response.is_complete_response() and response.is_valid_response():
                    try:
                        answers[query_tuple] = answer_cls(query_tuple[0], query_tuple[1], response.message, server)
                    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN), e:
                        answers[query_tuple] = e
                # if we received a message that was invalid or if there was
                # some error other than a timeout or network error, then label
                # the server invalid
                elif response.message is not None or \
                        response.error not in (query.RESPONSE_ERROR_TIMEOUT, query.RESPONSE_ERROR_NETWORK_ERROR):
                    valid_servers[query_tuple].remove(server)

            tuples_to_query = query_tuples.difference(answers)

        for query_tuple in tuples_to_query:
            answers[query_tuple] = dns.exception.Timeout()

        return answers

def main():
    import sys
    import getopt

    def usage():
        sys.stderr.write('Usage: %s <name> <type> [<server>...]\n' % (sys.argv[0]))
        sys.exit(1)

    try:
        opts, args = getopt.getopt(sys.argv[1:], '')
        opts = dict(opts)
    except getopt.error:
        usage()

    if len(args) < 2:
        usage()

    if len(args) < 3:
        r = get_standard_resolver()
    else:
        r = Resolver(map(IPAddr, sys.argv[3:]), query.StandardRecursiveQuery)
    a = r.query(dns.name.from_text(args[0]), dns.rdatatype.from_text(args[1]))

    print 'Response for %s/%s:' % (args[0], args[1])
    print '   from %s: %s (%d bytes)' % (a.server, repr(a.response), len(a.response.to_wire()))
    print '   answer:\n      %s' % (a.rrset)

if __name__ == '__main__':
    main()
