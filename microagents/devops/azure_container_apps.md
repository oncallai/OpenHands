---
name: azure_container_apps
type: knowledge
version: 1.0.0
agent: CodeActAgent
triggers:
- azure container app
- aca
- azure container
---

# Azure Container Apps Quickstart

## Prerequisites

- Azure CLI installed and logged in
- Azure subscription

## Install Azure CLI (if needed)

```bash
# On Debian/Ubuntu
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
```

## Register Azure Container Apps CLI extension

```bash
az extension add --name containerapp
```

## Create a Resource Group

```bash
az group create --name my-resource-group --location eastus
```

## Create a Container App Environment

```bash
az containerapp env create \
  --name my-environment \
  --resource-group my-resource-group \
  --location eastus
```

## Deploy a Container App

```bash
az containerapp create \
  --name my-app \
  --resource-group my-resource-group \
  --environment my-environment \
  --image mcr.microsoft.com/azuredocs/containerapps-helloworld:latest \
  --target-port 80 --ingress 'external'
```

## View the App URL

```bash
az containerapp show --name my-app --resource-group my-resource-group --query properties.configuration.ingress.fqdn
```
