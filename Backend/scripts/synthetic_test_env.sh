#!/bin/sh
set -eu

# Deliberately fake values for tests. Keep test execution independent from
# Doppler and from every real local/production credential source.
export DATABASE_URL='postgresql://synthetic_user:SYNTHETIC_TEST_VALUE@127.0.0.1:5432/camino_synthetic_app'
export TEST_DATABASE_URL='postgresql://synthetic_user:SYNTHETIC_TEST_VALUE@127.0.0.1:5432/camino_synthetic_test'
export CLERK_WH_KEY='whsec_SYNTHETIC_TEST_VALUE'
export CLERK_SECRET_KEY='sk_test_SYNTHETIC_TEST_VALUE'
export GH_APP_ID='123456789'
export GH_APP_CLIENT_ID='Iv1.SYNTHETIC_TEST_VALUE'
export GH_APP_SECRET='SYNTHETIC_TEST_VALUE_GH_APP_SECRET'
export GH_APP_PRIVATE_KEY='SYNTHETIC_TEST_VALUE_GH_APP_PRIVATE_KEY'
# Valid Fernet formatting for 32 zero bytes; not a real credential.
export ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='
export GH_WEBHOOK_SECRET='SYNTHETIC_TEST_VALUE_GH_WEBHOOK_SECRET'
export OPENAI_API_KEY='sk-SYNTHETIC_TEST_VALUE'

exec "$@"
