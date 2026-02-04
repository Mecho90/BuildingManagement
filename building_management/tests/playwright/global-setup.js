const dotenv = require('dotenv');

dotenv.config({ path: '.env' });

module.exports = async () => {
  // No-op: env loading handled above. Storage state is managed per test.
};
