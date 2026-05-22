module.exports = {
  apps: [
    {
      name: 'cloudnote-ingestion',
      script: '-m',
      args: 'app.main',
      interpreter: 'python3',
      cwd: '/opt/cloudnote-ingestion',
      env: {
        PYTHONUNBUFFERED: '1',
        LOG_LEVEL: 'INFO'
      },
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 10,
      error_file: 'logs/pm2_ingestion_error.log',
      out_file: 'logs/pm2_ingestion_out.log',
      merge_logs: true
    }
  ]
};
