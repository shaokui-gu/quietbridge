"""Validate the identity of an optional Developer ID provisioning profile."""
import argparse
import datetime
from pathlib import Path
import plistlib
import subprocess


def validate_values(values, identifier, team, certificate=None):
    entitlements = values.get('Entitlements', {})
    if (entitlements.get('com.apple.application-identifier') != team + '.' + identifier
            or entitlements.get('com.apple.developer.team-identifier') != team
            or values.get('TeamIdentifier') != [team]):
        raise ValueError('Provisioning profile does not match the expected bundle identifier/team')
    expiry = values.get('ExpirationDate')
    if not isinstance(expiry, datetime.datetime):
        raise ValueError('Provisioning profile has no valid expiration date')
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    if expiry <= datetime.datetime.now(datetime.timezone.utc):
        raise ValueError('Provisioning profile has expired')
    if certificate is not None and certificate not in values.get('DeveloperCertificates', []):
        raise ValueError('Provisioning profile does not include the supplied signing certificate')


def load_profile(path, identifier, team, certificate=None):
    path = Path(path)
    raw = path.read_bytes()
    decoded = subprocess.run(['/usr/bin/security', 'cms', '-D'], input=raw,
                             check=True, capture_output=True)
    validate_values(plistlib.loads(decoded.stdout), identifier, team, certificate)
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--identifier', required=True)
    parser.add_argument('--team', required=True)
    args = parser.parse_args()
    load_profile(args.profile, args.identifier, args.team)
    print('Provisioning profile identity and expiration verified')


if __name__ == '__main__':
    main()
