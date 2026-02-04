require('dotenv').config({ path: '.env' });

function credentials() {
  const username = process.env.E2E_TODO_USERNAME;
  const password = process.env.E2E_TODO_PASSWORD;
  if (username && password) {
    return { username, password };
  }
  return null;
}

module.exports = { credentials };
