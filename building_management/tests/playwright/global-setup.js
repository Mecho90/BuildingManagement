const { spawnSync } = require('child_process');
const path = require('path');
const { ensurePlaywrightEnv } = require('./utils/env');

module.exports = async () => {
  ensurePlaywrightEnv();
  const projectRoot = path.resolve(__dirname, '..', '..');
  const migrate = spawnSync('python3', ['manage.py', 'migrate', '--noinput'], {
    cwd: projectRoot,
    stdio: 'inherit',
  });
  if (migrate.error) {
    throw migrate.error;
  }
  if (migrate.status !== 0) {
    throw new Error('Failed to apply migrations before Playwright smoke tests.');
  }

  const result = spawnSync('python3', ['manage.py', 'seed_playwright'], {
    cwd: projectRoot,
    stdio: 'inherit',
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error('Failed to seed data for Playwright smoke tests.');
  }
};
