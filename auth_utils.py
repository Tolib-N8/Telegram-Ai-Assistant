import os
import json
import base64
from pathlib import Path
from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
    base64url_to_bytes,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    AuthenticatorAttachment,
    RegistrationCredential,
    AuthenticationCredential,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
)

PASSKEYS_FILE = Path("accounts/passkeys.json")

def load_passkeys():
    if not PASSKEYS_FILE.exists():
        return {}
    try:
        with open(PASSKEYS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_passkeys(data):
    PASSKEYS_FILE.parent.mkdir(exist_ok=True)
    with open(PASSKEYS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_webauthn_registration_options(username, display_name, rp_id=None):
    passkeys = load_passkeys()
    user_passkeys = passkeys.get(username, [])
    
    exclude_credentials = []
    for pk in user_passkeys:
        exclude_credentials.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(pk["credential_id"]),
                type=PublicKeyCredentialType.PUBLIC_KEY
            )
        )

    options = generate_registration_options(
        rp_id=rp_id or os.getenv("RP_ID", "localhost"),
        rp_name="Telegram Assistant",
        user_id=username.encode("utf-8"),
        user_name=username,
        user_display_name=display_name,
        exclude_credentials=exclude_credentials,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # authenticator_attachment=AuthenticatorAttachment.PLATFORM, # Removed to allow all types (Keys, Phone, etc)
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return options

def verify_webauthn_registration(username, response_data, challenge):
    try:
        # The webauthn library expects the challenge as raw bytes (base64url-decoded).
        expected_challenge = challenge
        if isinstance(challenge, str):
            try:
                expected_challenge = base64url_to_bytes(challenge)
            except Exception:
                expected_challenge = challenge

        # Allow caller to override expected_origin and rp_id via kwargs in newer callers
        expected_origin = response_data.get('_expected_origin') if isinstance(response_data, dict) and response_data.get('_expected_origin') else os.getenv("EXPECTED_ORIGIN", "http://localhost:8000")
        expected_rp_id = response_data.get('_expected_rp_id') if isinstance(response_data, dict) and response_data.get('_expected_rp_id') else os.getenv("RP_ID", "localhost")

        # Clean up internal keys if they exist so they don't confuse webauthn lib (though it might ignore them)
        if isinstance(response_data, dict):
            cleanup_data = response_data.copy()
            cleanup_data.pop('_expected_origin', None)
            cleanup_data.pop('_expected_rp_id', None)
        else:
            cleanup_data = response_data

        verification = verify_registration_response(
            credential=cleanup_data,
            expected_challenge=expected_challenge,
            expected_origin=expected_origin,
            expected_rp_id=expected_rp_id,
        )
        
        passkeys = load_passkeys()
        if username not in passkeys:
            passkeys[username] = []
            
        passkeys[username].append({
            "credential_id": base64.urlsafe_b64encode(verification.credential_id).decode("utf-8").replace("=", ""),
            "public_key": base64.urlsafe_b64encode(verification.credential_public_key).decode("utf-8").replace("=", ""),
            "sign_count": verification.sign_count,
            "transports": response_data.get("response", {}).get("transports", []),
        })
        save_passkeys(passkeys)
        return True
    except Exception as e:
        print(f"Registration verification failed: {e}")
        return False

def get_webauthn_authentication_options(username, rp_id=None):
    passkeys = load_passkeys()
    user_passkeys = passkeys.get(username, [])
    
    if not user_passkeys:
        return None

    allow_credentials = []
    for pk in user_passkeys:
        allow_credentials.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(pk["credential_id"]),
                type=PublicKeyCredentialType.PUBLIC_KEY
            )
        )

    options = generate_authentication_options(
        rp_id=rp_id or os.getenv("RP_ID", "localhost"),
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options

def verify_webauthn_authentication(username, response_data, challenge):
    passkeys = load_passkeys()
    user_passkeys = passkeys.get(username, [])
    
    # Find the specific credential
    cred_id = response_data.get("id")
    credential = next((pk for pk in user_passkeys if pk["credential_id"] == cred_id), None)
    
    if not credential:
        return False

    try:
        # Convert expected challenge to bytes if it's a base64url string
        expected_challenge = challenge
        if isinstance(challenge, str):
            try:
                expected_challenge = base64url_to_bytes(challenge)
            except Exception:
                expected_challenge = challenge

        expected_origin = response_data.get('_expected_origin') if isinstance(response_data, dict) and response_data.get('_expected_origin') else os.getenv("EXPECTED_ORIGIN", "http://localhost:8000")
        expected_rp_id = response_data.get('_expected_rp_id') if isinstance(response_data, dict) and response_data.get('_expected_rp_id') else os.getenv("RP_ID", "localhost")

        if isinstance(response_data, dict):
            cleanup_data = response_data.copy()
            cleanup_data.pop('_expected_origin', None)
            cleanup_data.pop('_expected_rp_id', None)
        else:
            cleanup_data = response_data

        verification = verify_authentication_response(
            credential=cleanup_data,
            expected_challenge=expected_challenge,
            expected_origin=expected_origin,
            expected_rp_id=expected_rp_id,
            credential_public_key=base64url_to_bytes(credential["public_key"]),
            credential_current_sign_count=credential["sign_count"],
        )
        
        # Update sign count
        credential["sign_count"] = verification.new_sign_count
        save_passkeys(passkeys)
        return True
    except Exception as e:
        print(f"Authentication verification failed: {e}")
        return False

