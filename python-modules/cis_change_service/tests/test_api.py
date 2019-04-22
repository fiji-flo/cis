import boto3
import json
import logging
import mock
import os
import random
import subprocess
import string
from cis_profile import common
from cis_profile import FakeUser
from cis_profile.fake_profile import FakeProfileConfig
from cis_profile import profile
from datetime import datetime
from datetime import timedelta
from tests.fake_auth0 import FakeBearer
from tests.fake_auth0 import json_form_of_pk

logging.basicConfig(level=logging.INFO, format="%(asctime)s:%(levelname)s:%(name)s:%(message)s")

logging.getLogger("boto").setLevel(logging.CRITICAL)
logging.getLogger("boto3").setLevel(logging.CRITICAL)
logging.getLogger("botocore").setLevel(logging.CRITICAL)


logger = logging.getLogger(__name__)


def get_complex_structures():
    return ["staff_information", "access_information", "identities", "schema"]


def ensure_appropriate_publishers_and_sign(fake_profile, publisher_rules, condition):
    os.environ["CIS_SECRET_MANAGER"] = "file"
    os.environ["CIS_SIGNING_KEY_NAME"] = "signing_key_name=fake-publisher-key_0.priv.pem"
    temp_profile = fake_profile
    complex_structures = get_complex_structures()
    for attr in publisher_rules[condition]:
        if attr == "primary_username" and temp_profile[attr]["value"] == "None":
            temp_profile[attr]["value"] = "".join(
                [random.choice(string.ascii_letters + string.digits) for n in range(32)]
            )

        if attr not in complex_structures:
            successful_random_publisher = random.choice(publisher_rules[condition][attr])
            temp_profile[attr]["signature"]["publisher"]["name"] = successful_random_publisher
            u = profile.User(user_structure_json=temp_profile)
            # Don't sign NULL attributes or invalid publishers
            if u._attribute_value_set(temp_profile[attr], strict=True) and (
                temp_profile[attr]["signature"]["publisher"]["name"] == successful_random_publisher
            ):
                u.sign_attribute(attr, successful_random_publisher)
            temp_profile = u.as_dict()
        else:
            if attr != "schema" and attr in complex_structures:
                for k in temp_profile[attr]:
                    try:
                        successful_random_publisher = random.choice(publisher_rules[condition][attr])
                    except KeyError:
                        successful_random_publisher = random.choice(publisher_rules[condition][attr][k])
                    temp_profile[attr][k]["signature"]["publisher"]["name"] = successful_random_publisher
                    u = profile.User(user_structure_json=temp_profile)
                    attribute = "{}.{}".format(attr, k)
                    # Don't sign NULL attributes or invalid publishers
                    if u._attribute_value_set(temp_profile[attr][k], strict=True) and (
                        temp_profile[attr][k]["signature"]["publisher"]["name"] == successful_random_publisher
                    ):
                        u.sign_attribute(attribute, successful_random_publisher)
                    temp_profile = u.as_dict()
    return profile.User(user_structure_json=temp_profile)


class TestAPI(object):
    def setup(self):
        os.environ["CIS_CONFIG_INI"] = "tests/mozilla-cis.ini"
        os.environ["AWS_XRAY_SDK_ENABLED"] = "false"

        from cis_change_service.common import get_config

        self.patcher_salt = mock.patch("cis_crypto.secret.AWSParameterstoreProvider.uuid_salt")
        self.mock_salt = self.patcher_salt.start()

        config = get_config()
        os.environ["CIS_DYNALITE_PORT"] = str(random.randint(32000, 34000))
        self.dynalite_port = config("dynalite_port", namespace="cis")
        self.dynaliteprocess = subprocess.Popen(["dynalite", "--port", self.dynalite_port], preexec_fn=os.setsid)

        name = "local-identity-vault"
        conn = boto3.client(
            "dynamodb",
            region_name="us-west-2",
            aws_access_key_id="ak",
            aws_secret_access_key="sk",
            endpoint_url="http://localhost:{}".format(self.dynalite_port),
        )
        try:
            conn.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "user_uuid", "AttributeType": "S"},
                    {"AttributeName": "sequence_number", "AttributeType": "S"},
                    {"AttributeName": "primary_email", "AttributeType": "S"},
                    {"AttributeName": "primary_username", "AttributeType": "S"},
                    {"AttributeName": "profile", "AttributeType": "S"},
                ],
                ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "{}-sequence_number".format(name),
                        "KeySchema": [{"AttributeName": "sequence_number", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                    },
                    {
                        "IndexName": "{}-primary_email".format(name),
                        "KeySchema": [{"AttributeName": "primary_email", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                    },
                    {
                        "IndexName": "{}-primary_username".format(name),
                        "KeySchema": [{"AttributeName": "primary_username", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                    },
                    {
                        "IndexName": "{}-user_uuid".format(name),
                        "KeySchema": [{"AttributeName": "user_uuid", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                    },
                ],
            )
            waiter = conn.get_waiter("table_exists")
            waiter.wait(TableName="local-identity-vault", WaiterConfig={"Delay": 1, "MaxAttempts": 5})
        except Exception as e:
            logger.error("Table error: {}".format(e))

        user_profile = FakeUser(config=FakeProfileConfig().minimal())
        self.user_profile = user_profile.as_json()
        from cis_change_service import api

        api.app.testing = True
        self.app = api.app.test_client()
        self.publisher_rules = common.WellKnown().get_publisher_rules()
        self.complex_structures = get_complex_structures()

    def test_index_exists(self):
        result = self.app.get("/v2", follow_redirects=True)
        assert result.status_code == 200

    @mock.patch("cis_change_service.idp.get_jwks")
    def test_stream_bypass_publishing_mode_it_should_succeed(self, fake_jwks):
        os.environ["CIS_STREAM_BYPASS"] = "true"
        os.environ["AWS_XRAY_SDK_ENABLED"] = "false"
        from cis_change_service import api

        f = FakeBearer()
        fake_jwks.return_value = json_form_of_pk
        token = f.generate_bearer_without_scope()
        api.app.testing = True
        self.app = api.app.test_client()
        result = self.app.post(
            "/v2/user",
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(self.user_profile),
            content_type="application/json",
            follow_redirects=True,
        )

        json.loads(result.get_data())
        assert result.status_code == 200

    @mock.patch("cis_change_service.idp.get_jwks")
    def test_change_endpoint_fails_with_invalid_token_and_jwt_validation_false(self, fake_jwks):
        os.environ["AWS_XRAY_SDK_ENABLED"] = "false"
        from cis_change_service import api

        os.environ["CIS_JWT_VALIDATION"] = "false"
        f = FakeBearer()
        bad_claims = {
            "iss": "https://auth-dev.mozilla.auth0.com/",
            "sub": "mc1l0G4sJI2eQfdWxqgVNcRAD9EAgHib@clients",
            "aud": "https://hacks",
            "iat": (datetime.utcnow() - timedelta(seconds=3100)).strftime("%s"),
            "exp": (datetime.utcnow() - timedelta(seconds=3100)).strftime("%s"),
            "scope": "read:allthething",
            "gty": "client-credentials",
        }

        fake_jwks.return_value = json_form_of_pk
        token = f.generate_bearer_with_scope("read:profile", bad_claims)
        api.app.testing = True
        self.app = api.app.test_client()
        result = self.app.get(
            "/v2/user",
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(self.user_profile),
            content_type="application/json",
            follow_redirects=True,
        )

        assert result.status_code == 200

    @mock.patch("cis_change_service.idp.get_jwks")
    def test_partial_update_it_should_fail(self, fake_jwks):
        os.environ["CIS_STREAM_BYPASS"] = "true"
        os.environ["AWS_XRAY_SDK_ENABLED"] = "false"
        os.environ["CIS_VERIFY_PUBLISHERS"] = "true"
        from cis_change_service import api

        fake_new_user = FakeUser(config=FakeProfileConfig().minimal())
        # Create a brand new user
        patched_user_profile = ensure_appropriate_publishers_and_sign(
            fake_new_user.as_dict(), self.publisher_rules, "create"
        )

        f = FakeBearer()
        fake_jwks.return_value = json_form_of_pk
        token = f.generate_bearer_without_scope()
        api.app.testing = True
        self.app = api.app.test_client()
        result = self.app.post(
            "/v2/user",
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(patched_user_profile.as_json()),
            content_type="application/json",
            follow_redirects=True,
        )

        response = json.loads(result.get_data())
        assert result.status_code == 200
        assert response["condition"] == "create"

        logger.info("A stub user has been created and verified to exist.")

        logger.info("Attempting failing partial update.")
        null_profile = profile.User(user_structure_json=None)
        null_profile.alternative_name.value = "iamanewpreferredlastname"
        null_profile.sign_attribute("alternative_name", "mozilliansorg")
        null_profile.user_id.value = "ad|wrong|LDAP"
        null_profile.active.value = True
        null_profile.sign_attribute("active", "access_provider")

        result = self.app.post(
            "/v2/user?user_id={}".format("mismatching_user_id"),
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(null_profile.as_json()),
            content_type="application/json",
            follow_redirects=True,
        )
        response = json.loads(result.get_data())
        assert result.status_code == 400

    @mock.patch("cis_change_service.idp.get_jwks")
    def test_partial_update_it_should_succeed(self, fake_jwks):
        os.environ["CIS_STREAM_BYPASS"] = "true"
        os.environ["AWS_XRAY_SDK_ENABLED"] = "false"
        os.environ["CIS_VERIFY_PUBLISHERS"] = "true"
        from cis_change_service import api

        fake_new_user = FakeUser(config=FakeProfileConfig().minimal())
        # Create a brand new user
        patched_user_profile = ensure_appropriate_publishers_and_sign(
            fake_new_user.as_dict(), self.publisher_rules, "create"
        )

        f = FakeBearer()
        fake_jwks.return_value = json_form_of_pk
        token = f.generate_bearer_without_scope()
        api.app.testing = True
        self.app = api.app.test_client()
        result = self.app.post(
            "/v2/user",
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(patched_user_profile.as_json()),
            content_type="application/json",
            follow_redirects=True,
        )

        response = json.loads(result.get_data())
        assert result.status_code == 200
        assert response["condition"] == "create"

        logger.info("A stub user has been created and verified to exist.")
        logger.info("Attempting partial update.")

        # Now let's try a partial update :)
        null_profile = profile.User(user_structure_json=None)
        null_profile.active.value = True
        null_profile.sign_attribute("active", "access_provider")
        null_profile.last_name.value = "iamanewpreferredlastname"
        null_profile.sign_attribute("last_name", "mozilliansorg")

        result = self.app.post(
            "/v2/user?user_id={}".format(patched_user_profile.user_id.value),
            headers={"Authorization": "Bearer " + token},
            data=json.dumps(null_profile.as_json()),
            content_type="application/json",
            follow_redirects=True,
        )
        logger.info(result.get_data())
        response = json.loads(result.get_data())
        assert result.status_code == 200
        assert response["condition"] == "update"

    def teardown(self):
        os.killpg(os.getpgid(self.dynaliteprocess.pid), 15)
        self.patcher_salt.stop()
