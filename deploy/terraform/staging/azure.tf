# eu-west agent on Microsoft Azure (West Europe, Netherlands).

resource "azurerm_resource_group" "agents" {
  count    = length(local.azure_agents) > 0 ? 1 : 0
  name     = "probeatlas-${var.environment}-agents"
  location = "westeurope"
  tags     = local.tags
}

resource "azurerm_virtual_network" "agent" {
  for_each            = local.azure_agents
  name                = "${each.value.name}-vnet"
  address_space       = ["10.60.0.0/24"]
  location            = azurerm_resource_group.agents[0].location
  resource_group_name = azurerm_resource_group.agents[0].name
  tags                = local.tags
}

resource "azurerm_subnet" "agent" {
  for_each             = local.azure_agents
  name                 = "agents"
  resource_group_name  = azurerm_resource_group.agents[0].name
  virtual_network_name = azurerm_virtual_network.agent[each.key].name
  address_prefixes     = ["10.60.0.0/26"]
}

resource "azurerm_network_security_group" "agent" {
  for_each            = local.azure_agents
  name                = "${each.value.name}-nsg"
  location            = azurerm_resource_group.agents[0].location
  resource_group_name = azurerm_resource_group.agents[0].name
  tags                = local.tags

  dynamic "security_rule" {
    for_each = length(var.operator_cidrs) > 0 ? [1] : []
    content {
      name                       = "ssh-from-operators"
      priority                   = 100
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "22"
      source_address_prefixes    = var.operator_cidrs
      destination_address_prefix = "*"
    }
  }
}

# A public IP gives the VM outbound internet access (new virtual networks have no default outbound access).
resource "azurerm_public_ip" "agent" {
  for_each            = local.azure_agents
  name                = "${each.value.name}-ip"
  location            = azurerm_resource_group.agents[0].location
  resource_group_name = azurerm_resource_group.agents[0].name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_network_interface" "agent" {
  for_each            = local.azure_agents
  name                = "${each.value.name}-nic"
  location            = azurerm_resource_group.agents[0].location
  resource_group_name = azurerm_resource_group.agents[0].name
  tags                = local.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.agent[each.key].id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.agent[each.key].id
  }
}

resource "azurerm_network_interface_security_group_association" "agent" {
  for_each                  = local.azure_agents
  network_interface_id      = azurerm_network_interface.agent[each.key].id
  network_security_group_id = azurerm_network_security_group.agent[each.key].id
}

resource "azurerm_linux_virtual_machine" "agent" {
  for_each              = local.azure_agents
  name                  = each.value.name
  location              = azurerm_resource_group.agents[0].location
  resource_group_name   = azurerm_resource_group.agents[0].name
  size                  = var.azure_agent_size
  admin_username        = "probeatlas"
  network_interface_ids = [azurerm_network_interface.agent[each.key].id]
  custom_data           = base64encode(local.agent_user_data[each.key])
  tags                  = merge(local.tags, { region_label = each.key })

  admin_ssh_key {
    username   = "probeatlas"
    public_key = var.ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 30
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  lifecycle {
    ignore_changes = [custom_data]
  }
}
