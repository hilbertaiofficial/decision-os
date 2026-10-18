"""Firebase Authentication: Google sign-in in the browser, ID-token verification on the server."""
import os


def configured():
    return bool(os.getenv('FIREBASE_API_KEY') and project())


def project():
    return os.getenv('FIREBASE_PROJECT_ID') or os.getenv('GOOGLE_CLOUD_PROJECT')


def client_config():
    """Public web config rendered into the login page. These values are not secrets."""
    if not configured():return None
    return {'apiKey':os.environ['FIREBASE_API_KEY'],'authDomain':os.getenv('FIREBASE_AUTH_DOMAIN',project()+'.firebaseapp.com'),'projectId':project()}


def _app():
    import firebase_admin
    try:return firebase_admin.get_app()
    except ValueError:return firebase_admin.initialize_app(options={'projectId':project()})


def verify(id_token):
    """Return {email, name} for a valid token with a verified email, else raise ValueError."""
    if not id_token or len(id_token)>4096:raise ValueError('Missing sign-in token.')
    from firebase_admin import auth
    try:claims=auth.verify_id_token(id_token,app=_app(),clock_skew_seconds=10)
    except Exception:raise ValueError('Sign-in token rejected.') from None
    if not claims.get('email') or not claims.get('email_verified'):raise ValueError('Use an account with a verified email address.')
    return {'email':claims['email'],'name':claims.get('name','')}
