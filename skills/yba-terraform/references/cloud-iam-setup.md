# Cloud IAM for YBA (upstream Terraform)

YBA needs cloud credentials to **deploy universe nodes** (create/terminate instances, attach volumes, manage security groups, etc.). These manifests use the **`aws` / `google` / `azurerm`+`azuread` providers** — *not* the `yba` provider — to create the least-privilege identity, then you feed its credentials into the matching `yba_*_provider` (see `providers-universe.md`).

> Installing YBA *itself* on a cloud VM needs **no** cloud permissions. The permissions below are only for node deployment. For **S3/GCS/Azure-blob backups**, also grant object read/write on the backup bucket.

Permission sources:
- AWS: <https://docs.yugabyte.com/stable/yugabyte-platform/prepare/cloud-permissions/cloud-permissions-nodes-aws/>
- GCP: <https://docs.yugabyte.com/stable/yugabyte-platform/prepare/cloud-permissions/cloud-permissions-nodes-gcp/>
- Azure: <https://docs.yugabyte.com/stable/yugabyte-platform/prepare/cloud-permissions/cloud-permissions-nodes-azure/>

---

## AWS — IAM user with the published node-deployment policy

The Yugabyte docs publish an exact EC2 policy. (Alternative to an access-key user: attach this policy to an IAM **role** on the YBA EC2 instance and set `use_iam_instance_profile = true` on `yba_aws_provider`.)

```hcl
resource "aws_iam_user" "yba" {
  name = "yba-node-deployer"
}

resource "aws_iam_user_policy" "yba_ec2" {
  name = "yba-ec2-node-deployment"
  user = aws_iam_user.yba.name

  # Verbatim from the Yugabyte AWS node-permissions doc.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "YBANodeDeployment"
      Effect = "Allow"
      Action = [
        "ec2:AttachVolume", "ec2:AuthorizeSecurityGroupIngress", "ec2:ImportVolume",
        "ec2:ModifyVolumeAttribute", "ec2:DescribeInstances", "ec2:DescribeInstanceAttribute",
        "ec2:CreateKeyPair", "ec2:DescribeVolumesModifications", "ec2:DeleteVolume",
        "ec2:DescribeVolumeStatus", "ec2:StartInstances", "ec2:DescribeAvailabilityZones",
        "ec2:DescribeSnapshots", "ec2:DescribeVolumes", "ec2:ModifyInstanceAttribute",
        "ec2:DescribeKeyPairs", "ec2:DescribeInstanceStatus", "ec2:DetachVolume",
        "ec2:ModifyVolume", "ec2:TerminateInstances", "ec2:AssignIpv6Addresses",
        "ec2:ImportKeyPair", "ec2:DescribeTags", "ec2:CreateTags", "ec2:RunInstances",
        "ec2:AssignPrivateIpAddresses", "ec2:StopInstances", "ec2:AllocateAddress",
        "ec2:DescribeVolumeAttribute", "ec2:DescribeSecurityGroups", "ec2:CreateVolume",
        "ec2:EnableVolumeIO", "ec2:DescribeImages", "ec2:DescribeVpcs",
        "ec2:DeleteSecurityGroup", "ec2:DescribeSubnets", "ec2:DeleteKeyPair",
        "ec2:DescribeVpcPeeringConnections", "ec2:DescribeRouteTables",
        "ec2:DescribeInternetGateways", "ec2:GetConsoleOutput", "ec2:CreateSnapshot",
        "ec2:DeleteSnapshot", "ec2:DescribeInstanceTypes"
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_access_key" "yba" {
  user = aws_iam_user.yba.name
}

output "yba_aws_access_key_id"     { value = aws_iam_access_key.yba.id }
output "yba_aws_secret_access_key" { value = aws_iam_access_key.yba.secret, sensitive = true }
```

For **encryption-at-rest via AWS KMS**, additionally grant: `kms:CreateKey`, `kms:ListAliases`, `kms:ListKeys`, `kms:CreateAlias`, `kms:DeleteAlias`, `kms:UpdateAlias`, `kms:TagResource`.

---

## GCP — service account with Compute Admin

The docs require the predefined role **`roles/compute.admin`**. (Alternative to a JSON key: attach this service account to the YBA VM and set `use_host_credentials = true` on `yba_gcp_provider`.)

```hcl
resource "google_service_account" "yba" {
  account_id   = "yba-node-deployer"
  display_name = "YBA node deployment"
}

resource "google_project_iam_member" "yba_compute_admin" {
  project = var.project_id
  role    = "roles/compute.admin"
  member  = "serviceAccount:${google_service_account.yba.email}"
}

# JSON key to upload to the GCP provider in YBA (credentials = file(...) / jsondecode).
resource "google_service_account_key" "yba" {
  service_account_id = google_service_account.yba.name
}

# Write the key out for the yba_gcp_provider `credentials` argument.
resource "local_sensitive_file" "yba_key" {
  filename = "${path.module}/yba-gcp-sa.json"
  content  = base64decode(google_service_account_key.yba.private_key)
}
```

For **GCS backups** also grant `roles/storage.admin` (or object-level read/write) on the backup bucket.

---

## Azure — app registration / service principal with role assignments

The docs require, on the resource group: **Network Contributor** + **Virtual Machine Contributor**.

```hcl
data "azurerm_subscription" "current" {}
data "azuread_client_config" "current" {}

resource "azuread_application" "yba" {
  display_name = "yba-node-deployer"
}

resource "azuread_service_principal" "yba" {
  client_id = azuread_application.yba.client_id
}

resource "azuread_application_password" "yba" {
  application_id = azuread_application.yba.id   # provider v2.47+: application_id; older: application_object_id
}

resource "azurerm_role_assignment" "yba_network" {
  scope                = azurerm_resource_group.yba.id   # or data.azurerm_resource_group.existing.id
  role_definition_name = "Network Contributor"
  principal_id         = azuread_service_principal.yba.object_id
}

resource "azurerm_role_assignment" "yba_vm" {
  scope                = azurerm_resource_group.yba.id
  role_definition_name = "Virtual Machine Contributor"
  principal_id         = azuread_service_principal.yba.object_id
}

# These map to the yba_azure_provider arguments:
output "azure_client_id"       { value = azuread_application.yba.client_id }
output "azure_client_secret"   { value = azuread_application_password.yba.value, sensitive = true }
output "azure_tenant_id"       { value = data.azuread_client_config.current.tenant_id }
output "azure_subscription_id" { value = data.azurerm_subscription.current.subscription_id }
```

If the YBA VM uses a **managed identity** instead, skip the client secret and set `use_managed_identity = true` on `yba_azure_provider`; assign the same two roles to the VM's identity.
