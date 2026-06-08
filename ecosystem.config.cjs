module.exports = {
  apps: [{
    name: 'robin',
    script: './main.py',
    interpreter: 'python3',
    args: '--cron --interval 600',
    cwd: __dirname,
    watch: false,
    autorestart: true,
    max_restarts: 5,
    env: {
      PYTHONUNBUFFERED: '1',
    },
  }],
};
