const path = require('path');

let loaded = false;

function ensurePlaywrightEnv() {
  if (loaded) {
    return;
  }
  loaded = true;

  try {
    const dotenv = require('dotenv');
    // Load base .env if present, then override with a dedicated Playwright file.
    dotenv.config({ path: path.resolve(process.cwd(), '.env') });
    dotenv.config({ path: path.resolve(process.cwd(), '.env.playwright') });
  } catch (error) {
    // Fail gracefully if dotenv is not installed yet; the auth helper will still error later
    // with a clear message when credentials are required.
    if (process.env.NODE_ENV !== 'test') {
      console.warn('[playwright] Unable to load dotenv configuration:', error.message);
    }
  }
}

module.exports = {
  ensurePlaywrightEnv,
};
