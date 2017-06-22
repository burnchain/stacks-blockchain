#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack-client
    ~~~~~
    copyright: (c) 2017 by Blockstack.org

    This file is part of Blockstack-client.

    Blockstack-client is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack-client is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack-client. If not, see <http://www.gnu.org/licenses/>.
"""

import base64, copy
import ecdsa, hashlib
import keylib
from itertools import izip
from blockstack_client import data, storage
from blockstack_client import zonefile as bs_zonefile
from blockstack_client import user as user_db
from blockstack_client.logger import get_logger
import blockstack_zones
from blockstack_client.rpc import local_api_connect

log = get_logger()

SUBDOMAIN_ZF_PARTS = "zf-parts"
SUBDOMAIN_ZF_PIECE = "zf%d"
SUBDOMAIN_SIG = "sig"
SUBDOMAIN_PUBKEY = "pub-key"
SUBDOMAIN_N = "sequence-n"

class ParseError(Exception):
    pass

class SubdomainNotFound(Exception):
    pass

class SubdomainNotFound(Exception):
    pass

class SubdomainAlreadyExists(Exception):
    pass


def decode_pubkey_entry(pubkey_entry):
    assert pubkey_entry.startswith("data:")
    pubkey_entry = pubkey_entry[len("data:"):]
    header, data = pubkey_entry.split(":")

    if header == "echex":
        return keylib.ECPublicKey(data)

    return header, data

def encode_pubkey_entry(key):
    """
    key should be a key object, right now this means 
        keylib.ECPrivateKey or
        keylib.ECPublicKey
    """
    if isinstance(key, keylib.ECPrivateKey):
        data = key.public_key().to_hex()
        head = "echex"
    elif isinstance(key, keylib.ECPublicKey):
        data = key.to_hex()
        head = "echex"
    else:
        raise NotImplementedError("No support for this key type")

    return "data:{}:{}".format(head, data)

def txt_encode_key_value(key, value):
    return "{}={}".format(key,
                          value.replace("=", "\\="))

class Subdomain(object):
    def __init__(self, name, pubkey_encoded, n, zonefile_str, sig=None):
        self.name = name
        self.pubkey = decode_pubkey_entry(pubkey_encoded)
        self.n = n
        self.zonefile_str = zonefile_str
        self.sig = sig

    def pack_subdomain(self):
        """ Returns subdomain packed into a list of strings
            Also defines the canonical order for signing!
            PUBKEY, N, ZF_PARTS, IN_ORDER_PIECES
        """
        output = []
        output.append(txt_encode_key_value(SUBDOMAIN_PUBKEY, 
                                           encode_pubkey_entry(self.pubkey)))
        output.append(txt_encode_key_value(SUBDOMAIN_N, "{}".format(self.n)))
        
        encoded_zf = base64.b64encode(self.zonefile_str)
        # let's pack into 250 byte strings -- the entry "zf99=" eliminates 5 useful bytes,
        # and the max is 255.
        n_pieces = (len(encoded_zf) / 250) + 1
        if len(encoded_zf) % 250 == 0:
            n_pieces -= 1
        output.append(txt_encode_key_value(SUBDOMAIN_ZF_PARTS, "{}".format(n_pieces)))
        for i in range(n_pieces):
            start = i * 250
            piece_len = min(250, len(encoded_zf[start:]))
            assert piece_len != 0
            piece = encoded_zf[start:(start+piece_len)]
            output.append(txt_encode_key_value(SUBDOMAIN_ZF_PIECE % i, piece))

        if self.sig is not None:
            output.append(txt_encode_key_value(SUBDOMAIN_SIG, self.sig))

        return output

    def add_signature(self, privkey):
        plaintext = self.get_plaintext_to_sign()
        self.sig = sign(privkey, plaintext)

    def verify_signature(self, pubkey):
        return verify(pubkey, self.get_plaintext_to_sign(), self.sig)

    def as_zonefile_entry(self):
        d = { "name" : self.name,
              "txt" : self.pack_subdomain() }
        return d

    def get_plaintext_to_sign(self):
        as_strings = self.pack_subdomain()
        if self.sig is not None:
            as_strings = as_strings[:-1]
        return "".join(as_strings)

    @staticmethod
    def parse_subdomain_record(rec):
        txt_entry = rec['txt']
        if not isinstance(txt_entry, list):
            raise ParseError("Tried to parse a TXT record with only a single <character-string>")
        entries = {}
        for item in txt_entry:
            first_equal = item.index("=")
            key = item[:first_equal]
            value = item[first_equal + 1:]
            value = value.replace("\\=", "=") # escape equals
            assert key not in entries
            entries[key] = value

        pubkey = entries[SUBDOMAIN_PUBKEY]
        n = entries[SUBDOMAIN_N]
        if SUBDOMAIN_SIG in entries:
            sig = entries[SUBDOMAIN_SIG]
        else:
            sig = None
        zonefile_parts = int(entries[SUBDOMAIN_ZF_PARTS])
        b64_zonefile = "".join([ entries[SUBDOMAIN_ZF_PIECE % zf_index] for
                                 zf_index in range(zonefile_parts) ])

        return Subdomain(rec['name'], pubkey, int(n),
                         base64.b64decode(b64_zonefile), sig)

# aaron: I was hesitant to write these two functions. But I did so because:
#   1> getting the sign + verify functions from virtualchain.ecdsa 
#      was tricky because of the hashfunc getting lost in translating from
#      SK to PK
#   2> didn't want this code to necessarily depend on virtualchain

def sign(sk, plaintext):
    signer = ecdsa.SigningKey.from_pem(sk.to_pem())
    blob = signer.sign_deterministic(plaintext, hashfunc = hashlib.sha256)
    return base64.b64encode(blob)

def verify(pk, plaintext, sigb64):
    signature = base64.b64decode(sigb64)
    verifier = ecdsa.VerifyingKey.from_pem(pk.to_pem())
    return verifier.verify(signature, plaintext, hashfunc = hashlib.sha256)

def is_subdomain_record(rec):
    txt_entry = rec['txt']
    if not isinstance(txt_entry, list):
        return False
    for entry in txt_entry:
        if entry.startswith(SUBDOMAIN_ZF_PARTS + "="):
            return True
    return False

def parse_zonefile_subdomains(zonefile_json):
    registrar_urls = []

    if "txt" in zonefile_json:
        subdomains = [ Subdomain.parse_subdomain_record(x) for x in zonefile_json["txt"]
                       if is_subdomain_record(x) ]
    else:
        subdomains = []

    return subdomains

def is_a_subdomain(fqa):
    """
    Tests whether fqa is a subdomain. 
    If it isn't, returns False.
    If it is, returns True and a tuple (subdomain_name, domain)
    """
    if re.match(schemas.OP_NAME_PATTERN, fqa) == None:
        return False
    pieces = fqa.split(".")
    if len(pieces) == 3:
        return (True, (pieces[0], ("{}.{}".format(*pieces[1:]))))
    return False

def _transition_valid(from_sub_record, to_sub_record):
    if from_sub_record.n + 1 != to_sub_record.n:
        log.warn("Failed subdomain {} transition because of N:{}->{}".format(
            to_sub_record.name, from_sub_record.n, to_sub_record.n))
        return False
    if not to_sub_record.verify_signature(from_sub_record.pubkey):
        log.warn("Failed subdomain {} transition because of signature failure".format(
            to_sub_record.name))
        return False
    return True

def _build_subdomain_db(domain_fqa, zonefiles):
    subdomain_db = {}
    for zf in zonefiles:
        zf_json = bs_zonefile.decode_name_zonefile(domain_fqa, zf)
        subdomains = parse_zonefile_subdomains(zf_json)

        for subdomain in subdomains:
            if subdomain.name in subdomain_db:
                previous = subdomain_db[subdomain.name]
                if _transition_valid(previous, subdomain):
                    subdomain_db[subdomain.name] = subdomain
                else:
                    log.warn("Failed subdomain transition for {}.{} on N:{}->{}".format(
                        subdomain.name, domain_fqa, previous.n, subdomain.n))
            else:
                if subdomain.n != 0:
                    log.warn("First sight of subdomain {}.{} with N={}".format(
                        subdomain.name, domain_fqa, subdomain.n))
                    continue
                subdomain_db[subdomain.name] = subdomain
    return subdomain_db

def flatten_and_issue_zonefile(domain_fqa, zf):
    user_data_txt = blockstack_zones.make_zone_file(zf)

    rpc = local_api_connect()
    assert rpc
    try:
        rpc.backend_update(domain_fqa, user_data_txt, None, None, None)
    except Exception as e:
        log.exception(e)
        return False

def _extend_with_subdomain(zf_json, subdomain):
    txt_data = subdomain.pack_subdomain()
    name = subdomain.name

    if "txt" not in zf_json:
        zf_json["txt"] = []

    txt_records = zf_json["txt"]

    for rec in txt_records:
        if name == rec["name"]:
            raise Exception("Name {} already exists in zonefile TXT records.".format(
                name))

    zf_json["txt"].append(subdomain.as_zonefile_entry())

def add_subdomain(subdomain, domain_fqa, key, zonefile):
    # step 1: see if this resolves to an already defined subdomain
    subdomain_already = True
    try:
        resolve_subdomain(subdomain, domain_fqa)
    except SubdomainNotFound as e:
        subdomain_already = False
    if subdomain_already:
        raise SubdomainAlreadyExists("{}.{}".format(subdomain, domain_fqa))
    # step 2: get domain's current zonefile and filter the subdomain entries
    zf = copy.deepcopy(bz_zonefile.get_name_zonefile(domain_fqa))
    zf["txt"] = list([ x for x in zf["txt"]
                    if not x["name"].startswith("_subd.")])
    # step 3: create a subdomain record

    subdomain_obj = Subdomain(subdomain, subdomains.encode_pubkey_entry(key),
                              0, zonefile)

    _extend_with_subdomain(zf, subdomain_obj)

    # step 4: issue zonefile update
    flatten_and_issue_zonefile(domain_fqa, zf)


def resolve_subdomain(subdomain, domain_fqa):
    # step 1: fetch domain zonefiles.
    zonefiles = data.list_zonefile_history(domain_fqa)

    # step 2: for each zonefile, parse the subdomain
    #         operations.
    subdomain_db = _build_subdomain_db(domain_fqa, zonefiles)

    # step 3: find the subdomain.
    if not subdomain in subdomain_db:
        raise SubdomainNotFound(subdomain)
    my_rec = subdomain_db[subdomain]

    # step 4: resolve!

    owner_pubkey = my_rec.pubkey

    parsed_zf = bs_zonefile.decode_name_zonefile(my_rec.name, my_rec.zonefile_str)
    urls = user_db.user_zonefile_urls(parsed_zf)

    try:
        user_data_pubkey = user_db.user_zonefile_data_pubkey(parsed_zf)
        if user_data_pubkey is not None:
            user_data_pubkey = str(user_data_pubkey)
    except ValueError:
        user_data_pubkey = owner_pubkey.to_hex()

    user_profile = storage.get_mutable_data(
        None, user_data_pubkey, blockchain_id=None,
        data_address=None, owner_address=None,
        urls=urls, drivers=None, decode=True,
    )

    return user_profile
