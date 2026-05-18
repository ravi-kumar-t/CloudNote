# Kubernetes Deployment Guide

This directory contains manifests for deploying the CloudNote Ingestion Worker to a Kubernetes cluster.

## Manifests
- `namespace.yaml`: Isolates the project.
- `configmap.yaml`: Non-sensitive configuration.
- `secret.yaml`: Secure credentials.
- `pvc.yaml`: Persistent storage for logs/screenshots.
- `cronjob.yaml`: Scheduled execution of the ingestion worker.

## Deployment Steps

1. **Create Namespace**:
   ```bash
   kubectl apply -f namespace.yaml
   ```

2. **Apply Configurations**:
   ```bash
   kubectl apply -f configmap.yaml
   kubectl apply -f secret.yaml
   ```

3. **Setup Storage**:
   ```bash
   kubectl apply -f pvc.yaml
   ```

4. **Deploy CronJob**:
   ```bash
   kubectl apply -f cronjob.yaml
   ```

## Local Testing (Minikube)
1. Point your shell to Minikube's Docker daemon:
   ```bash
   eval $(minikube docker-env)
   ```
2. Build the image locally:
   ```bash
   docker build -t cloudnote-ingestion:latest .
   ```
3. Apply the manifests as shown above.

## Monitoring
- Check CronJob status:
  ```bash
  kubectl get cronjob -n cloudnote
  ```
- View logs from the latest pod:
  ```bash
  kubectl logs -f -l job-name=<job-name> -n cloudnote
  ```
