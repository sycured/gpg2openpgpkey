#!/usr/bin/python
#
# Given a UID and a file containing a GPG key, generate the 
# corresponding OPENPGPKEY DNS record in presentation format.
#
# Tested with gnupg 1.x and 2.x.
#

import os, sys, time, rfc822, getopt
import tempfile, shutil, subprocess, threading, base64, hashlib
from tempfile import mkdtemp

PROGNAME    = os.path.basename(sys.argv[0])
VERSION     = "0.1"
GPG         = "gpg"
GPG_TIMEOUT = 5
DIR         = "/var/tmp/openpgpkey"
GPGDIR      = None
GENERIC     = False                            # Generate generic rdata?


# from gnupg2 source common/openpgpdefs.h
ALG_PUBKEY = {
    1: "RSA",
    2: "RSA Encrypt-Only",                     # Deprecated
    3: "RSA Sign-Only",                        # Deprecated
    16: "Elgamal Encrypt-Only",                # Deprecated
    17: "DSA",
    18: "ECDH",                                # Generic ECC
    19: "ECDSA",
    20: "Elgamal",
    21: "Diffie-Hellman",
    22: "Ed25519",                             # Squatting (not official)
}


def usage(msg=None):
    if msg: print("Error: {}\n".format(msg))
    print("""\
{0} version {1}
Usage: {0} [-g] [-h] <email> <gpgkeyfile>

       -g     Output DNS generic RDATA format (rather than OPENPGPKEY)
       -h     Print this help message

Given an email address and a file containing a GPG public key, this program
generates a corresponding DNS OPENPGPKEY resource record in presentation
format.
""".format(PROGNAME, VERSION))
    sys.exit(2)


def process_args(arguments):
    """Process command line arguments"""
    global GENERIC
    try:
        (options, args) = getopt.getopt(sys.argv[1:], 'g')
    except getopt.GetoptError:
        usage()

    if len(args) != 2:
        usage("Incorrect number of arguments.")

    for option, value in options:
        if option == "-g":
            GENERIC = True
        elif option == "-h":
            usage()

    return (args)


def stringchunks(s, n):
    """Yield n-octet sized chunks from string s"""
    for i in xrange(0, len(s), n):
        yield s[i:i+n]


def cmd_gpg_import(homedir):
    return [GPG, "--homedir", homedir, "--import"]


def cmd_gpg_listkeys(homedir):
    return [GPG, "--homedir", homedir,
            "--fixed-list-mode", "--with-colons",
            "--list-keys", "--with-fingerprint", "--with-fingerprint"]


def cmd_gpg_export(homedir, uid):
    return [GPG, "--homedir", homedir, "--export",
            "--export-options", "export-minimal,no-export-attributes", uid]


def unixtime2date(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def parse_key(indata, keydata):
    """Parse output of machine parseable list-keys command and return
    an OpenPGPKey class object.
    See gpg source doc/DETAILS for machine parseable format"""

    p = None
    pubkeySeen = False
    currentKey = None

    for line in indata.split('\n'):
        parts = line.split(':')
        rectype = parts[0]
        if rectype == 'tru':
            continue

        if rectype == 'pub':
            if pubkeySeen:
                error_quit(11, "ERROR: more than one public key given.")
            pubkeySeen = True
            _, _, keylength, alg, keyid, createDate, _, _, _, _, _, \
                keycap = parts[:12]
            p = OpenPGPKey(keyid, alg, keylength, keycap, createDate,
                           keydata=keydata)
            currentKey = p

        elif rectype == 'uid':
            userid = parts[9]
            if p:
                _, email = rfc822.parseaddr(userid)
                p.add_uid(userid)
            else:
                error_quit(11, "ERROR: uid found without preceding pubkey.")

        elif rectype == 'sub':
            if not pubkeySeen:
                error_quit(11, "ERROR: subkey without preceding pubkey.")
            _, _, keylength, alg, keyid, createDate, _, _, _, _, _, \
                keycap = parts[:12]
            s = OpenPGPKey(keyid, alg, keylength, keycap, createDate)
            currentKey = s
            p.add_subkey(s)

        elif rectype == 'fpr':
            fingerprint = parts[9]
            if p:
                currentKey.set_fingerprint(fingerprint)
            else:
                error_quit(11, "ERROR: fpr found without preceding pubkey.")
    return p


class OpenPGPKey:

    def __init__(self, keyid, alg, keylen, flags, date, keydata=None):
        self.keyid = keyid
        self.fingerprint = None
        self.alg = int(alg)
        self.keylen = int(keylen)
        self.flags = flags
        self.createDate = float(date)
        self.uidlist = []               # list of rfc822 (name, address) tuples
        self.subkeys = []               # list of OpenPGPKey objects
        if keydata:
            self.keydata = keydata
        else:
            self.keydata = None

    def set_fingerprint(self, fpr):
        self.fingerprint = fpr

    def add_uid(self, uid):
        name, address = rfc822.parseaddr(uid)
        self.uidlist.append((name, address))

    def add_subkey(self, subkey):
        self.subkeys.append(subkey)

    def has_uid(self, uid):
        name, address = rfc822.parseaddr(uid)
        return address in [x[1] for x in self.uidlist]

    def printInfo(self, subkey=False):
        print("OpenPGPKey:" if not subkey else "SubKey:")
        print("  keyid={} fpr={}".format(self.keyid, self.fingerprint))
        print("  algorithm={} ({}) keylen={} flags=[{}]".format(
            self.alg, ALG_PUBKEY.get(self.alg), self.keylen, self.flags))
        print("  CreateDate: {}".format(unixtime2date(self.createDate)))
        for u in self.uidlist:
            print("    uid: {}".format(rfc822.dump_address_pair(u)))
        for s in self.subkeys:
            s.printInfo(subkey=True)


class RunProgram(threading.Thread):

    def __init__(self, cmd, indata, timeout):
        threading.Thread.__init__(self)
        self.cmd = cmd
        self.indata = indata
        self.timeout = timeout
        self.output = ""

    def run(self):
        self.p = subprocess.Popen(self.cmd,
                                  stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        self.output = self.p.communicate(input=self.indata)[0]
        self.returncode = self.p.returncode

    def Start(self):
        self.start()
        self.join(self.timeout)
        if self.is_alive():
            self.p.terminate()
            self.join()
            self.output += "\nTimeout: No response in %d seconds\n" % self.timeout


def validate_uid(inputstring):
    name, email = rfc822.parseaddr(inputstring)
    if "@" in email:
        return email
    else:
        return None


def get_ownername(email):
    """Return OPENPGPKEY ownername for given PGP uid/email address"""
    localpart, rhs = email.split('@')
    h = hashlib.sha256()
    h.update(localpart)
    owner = "%s._openpgpkey.%s" % (h.hexdigest()[0:56], rhs)
    if not owner.endswith('.'):
        owner = owner + '.'
    return owner


def gen_openpgpkey(email, keydata, generic=False):
    owner = get_ownername(email)
    if not generic:
        output = "{} IN OPENPGPKEY (\n".format(owner)
        for line in stringchunks(base64.standard_b64encode(keydata), 60):
            output += "                  {}\n".format(line)
    else:
        output = "{} IN TYPE61 \# {} (".format(owner, len(keydata))
        for line in stringchunks(keydata.encode('hex'), 60):
            output += "                  {}\n".format(line)
    output += ")"
    return output


def rmtree(pathname):
    try:
        shutil.rmtree(pathname)
    except:
        # send this to syslog instead if cgi program
        print("Error: failed to remove temporary directory.")
        return False
    else:
        return True


def error_quit(rc, msg):
    print("ERROR: {}; rc={}".format(msg, rc))
    if GPGDIR and (not rmtree(GPGDIR)):
        print("ERROR: deleting temp dir: {}".format(GPGDIR))
    sys.exit(1)


if __name__ == '__main__':


    uid, infile = process_args(sys.argv[1:])
    uid = validate_uid(uid)
    if not uid:
        error_quit(11, "invalid uid specified", None)
    keydata = open(infile).read()

    GPGDIR = mkdtemp(prefix="x", dir=DIR)

    c = RunProgram(cmd_gpg_import(GPGDIR), keydata, GPG_TIMEOUT)
    c.Start()
    if c.returncode != 0:
        error_quit(c.returncode, "gpg import error")

    c = RunProgram(cmd_gpg_listkeys(GPGDIR), None, GPG_TIMEOUT)
    c.Start()
    if c.returncode != 0:
        error_quit(c.returncode, "gpg list_keys error")

    pgpkey = parse_key(c.output, keydata)
    if not pgpkey:
        error_quit(11, "couldn't parse openpgp key")
    pgpkey.printInfo()

    if not pgpkey.has_uid(uid):
        error_quit(11, "couldn't find uid {} in given key".format(uid))
    print('')

    c = RunProgram(cmd_gpg_export(GPGDIR, uid), None, GPG_TIMEOUT)
    c.Start()
    if c.returncode != 0:
        error_quit(c.returncode, "gpg export error")

    print(gen_openpgpkey(uid, c.output, generic=GENERIC))

    if not rmtree(GPGDIR):
        print("ERROR: deleting gpg directory: {}".format(GPGDIR))
        sys.exit(11)