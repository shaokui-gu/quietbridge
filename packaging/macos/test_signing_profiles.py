import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from signing_profiles import validate_values


class SigningProfileTests(unittest.TestCase):
    def values(self):
        return {
            'Entitlements': {
                'com.apple.application-identifier': '62RNP54C47.com.quietbridge.helper',
                'com.apple.developer.team-identifier': '62RNP54C47',
            },
            'TeamIdentifier': ['62RNP54C47'],
            'ExpirationDate': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
            'DeveloperCertificates': [b'expected-certificate'],
        }

    def test_current_identity_and_included_certificate_are_accepted(self):
        validate_values(self.values(), 'com.quietbridge.helper', '62RNP54C47', b'expected-certificate')

    def test_legacy_and_other_component_profiles_are_rejected(self):
        for identifier in ('com.workbuddy.cua.helper', 'com.quietbridge.app', 'com.quietbridge.AuthorizationPlugin'):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, 'identifier/team'):
                validate_values(self.values(), identifier, '62RNP54C47')

    def test_foreign_team_expired_profile_and_wrong_certificate_are_rejected(self):
        values = self.values()
        values['TeamIdentifier'] = ['OTHERTEAM1']
        with self.assertRaises(ValueError):
            validate_values(values, 'com.quietbridge.helper', '62RNP54C47')
        values = self.values()
        values['ExpirationDate'] = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, 'expired'):
            validate_values(values, 'com.quietbridge.helper', '62RNP54C47')
        with self.assertRaisesRegex(ValueError, 'certificate'):
            validate_values(self.values(), 'com.quietbridge.helper', '62RNP54C47', b'other-certificate')
