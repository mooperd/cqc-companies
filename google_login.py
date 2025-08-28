import os
import base64
import json
from flask import session, request, redirect, url_for
from requests_oauthlib import OAuth2Session
from app.telemetry import traced_function, get_tracer
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Google OAuth2 configuration
encoded_config = os.environ.get("GOOGLE_OAUTH2_CONFIG_B64")
google_oauth2_config = json.loads(base64.b64decode(encoded_config).decode('utf-8'))

client_id = google_oauth2_config["web"]["client_id"]
client_secret = google_oauth2_config["web"]["client_secret"]
authorization_base_url = google_oauth2_config["web"]["auth_uri"]
token_url = google_oauth2_config["web"]["token_uri"]
userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"

@traced_function(
    span_name="auth.google.get_redirect_uri",
    attributes=lambda callback_path: {"callback_path": callback_path}
)
def get_dynamic_redirect_uri(callback_path):
    host = (
        request.headers.get('X-Forwarded-Host') or
        request.headers.get('Host') or
        request.host
    )
    scheme = "http" if "localhost" in host or "127.0.0.1" in host else request.headers.get('X-Forwarded-Proto') or 'https'
    redirect_uri = f"{scheme}://{host}{callback_path}"
    logger.info(f"Dynamic redirect URI: {redirect_uri}")
    return redirect_uri

@traced_function(
    span_name="auth.google.start_login",
    attributes=lambda callback_path: {"callback_path": callback_path}
)
def start_google_login(callback_path):
    tracer = get_tracer()
    with tracer.start_as_current_span("auth.google.oauth_flow_start") as span:
        current_redirect_uri = get_dynamic_redirect_uri(callback_path)
        span.set_attribute("oauth.redirect_uri", current_redirect_uri)
        span.set_attribute("oauth.client_id", client_id[:10] + "...")  # Only log prefix for security
        
        google = OAuth2Session(client_id, scope=['openid', 'email', 'profile'], redirect_uri=current_redirect_uri)
        authorization_url, state = google.authorization_url(authorization_base_url, access_type='offline', prompt='select_account')
        
        session['oauth_state'] = state
        session['oauth_redirect_uri'] = current_redirect_uri
        
        span.set_attribute("oauth.state", state[:10] + "...")  # Only log prefix for security
        span.set_attribute("oauth.authorization_url", authorization_url[:50] + "...")  # Only log prefix
        
        logger.info(f"Starting OAuth flow with redirect URI: {current_redirect_uri}")
        return redirect(authorization_url)

@traced_function(span_name="auth.google.handle_callback")
def handle_google_callback():
    tracer = get_tracer()
    with tracer.start_as_current_span("auth.google.oauth_callback") as span:
        oauth_state = session.get('oauth_state')
        oauth_redirect_uri = session.get('oauth_redirect_uri')
        
        span.set_attribute("oauth.has_state", bool(oauth_state))
        span.set_attribute("oauth.redirect_uri", oauth_redirect_uri)
        
        google = OAuth2Session(client_id, state=oauth_state, redirect_uri=oauth_redirect_uri)
        
        try:
            # Fetch token
            token = google.fetch_token(token_url, client_secret=client_secret, authorization_response=request.url)
            session['oauth_token'] = token
            span.set_attribute("oauth.token_obtained", True)
            
            # Get user info
            resp = google.get(userinfo_url)
            user_info = resp.json()
            
            span.set_attribute("oauth.user_email", user_info.get('email', 'unknown'))
            span.set_attribute("oauth.user_name", user_info.get('name', 'unknown'))
            span.set_attribute("oauth.success", True)
            
            return user_info
            
        except Exception as e:
            span.set_attribute("oauth.success", False)
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            logger.error(f"OAuth callback error: {e}")
            raise