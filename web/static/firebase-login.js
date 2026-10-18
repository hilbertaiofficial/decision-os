// Firebase Authentication (Google provider). The ID token is posted to /auth/firebase,
// verified server-side with the Firebase Admin SDK, and exchanged for a DecisionOS session.
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js';
import { getAuth, GoogleAuthProvider, signInWithPopup } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';

const button = document.getElementById('firebase-google');
if (button) {
  const config = JSON.parse(button.dataset.config);
  const auth = getAuth(initializeApp(config));
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await signInWithPopup(auth, new GoogleAuthProvider());
      const form = document.getElementById('firebase-form');
      form.elements.id_token.value = await result.user.getIdToken();
      form.submit();
    } catch (error) {
      button.disabled = false;
      const note = document.getElementById('firebase-error');
      if (note) { note.hidden = false; note.textContent = 'Google sign-in did not complete. Try again.'; }
    }
  });
}
