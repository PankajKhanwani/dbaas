import { useState, useEffect } from 'react'
import './Providers.css'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1'

function Providers({ showNotification }) {
  const [providers, setProviders] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [selectedProvider, setSelectedProvider] = useState(null)

  const [newProvider, setNewProvider] = useState({
    name: '',
    region: '',
    availability_zone: '',
    cloud_provider: 'AWS',
    cpu_total_cores: 100,
    memory_total_gb: 200,
    storage_total_gb: 1000,
    cpu_reservation_percent: 80,
    memory_reservation_percent: 80,
    storage_reservation_percent: 80,
    kubeconfig_content: '',
    api_endpoint: '',
    verify_ssl: true,
    is_active: true,
    priority: 100,
    tags: {},
    metadata: {}
  })

  useEffect(() => {
    fetchProviders()
    const interval = setInterval(fetchProviders, 10000)
    return () => clearInterval(interval)
  }, [])

  const fetchProviders = async () => {
    try {
      console.log('Fetching providers from:', `${API_BASE}/providers/`)
      const response = await fetch(`${API_BASE}/providers/`)
      console.log('Response status:', response.status, response.statusText)
      
      if (!response.ok) {
        const errorText = await response.text()
        console.error('Error response:', errorText)
        throw new Error(`HTTP ${response.status}: ${response.statusText}`)
      }
      
      const data = await response.json()
      console.log('Providers data received:', data)
      console.log('Providers array:', data.providers)
      setProviders(data.providers || [])
      setLoading(false)
    } catch (error) {
      console.error('Error fetching providers:', error)
      setLoading(false)
      showNotification(`Failed to fetch providers: ${error.message}`, 'error')
    }
  }

  const handleFileUpload = (e) => {
    const file = e.target.files[0]
    if (file) {
      const reader = new FileReader()
      reader.onload = (event) => {
        setNewProvider({...newProvider, kubeconfig_content: event.target.result})
      }
      reader.readAsText(file)
    }
  }

  const validateForm = () => {
    const errors = []

    if (!newProvider.name.trim()) {
      errors.push('Provider name is required')
    }
    if (!newProvider.region.trim()) {
      errors.push('Region is required')
    }
    if (!newProvider.kubeconfig_content.trim()) {
      errors.push('Kubeconfig is required')
    }
    if (newProvider.cpu_total_cores < 1) {
      errors.push('CPU must be at least 1 core')
    }
    if (newProvider.memory_total_gb < 1) {
      errors.push('Memory must be at least 1 GB')
    }
    if (newProvider.storage_total_gb < 1) {
      errors.push('Storage must be at least 1 GB')
    }

    if (errors.length > 0) {
      showNotification(errors.join(', '), 'error')
      return false
    }
    return true
  }

  const handleCreate = async () => {
    if (!validateForm()) return

    try {
      const response = await fetch(`${API_BASE}/providers/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newProvider)
      })

      if (!response.ok) {
        let errorMessage = 'Failed to create provider'
        try {
          const error = await response.json()
          errorMessage = error.error?.message || error.message || errorMessage
          // Add more details if available
          if (error.error?.details) {
            const details = typeof error.error.details === 'string' 
              ? error.error.details 
              : JSON.stringify(error.error.details)
            errorMessage += `: ${details}`
          }
        } catch (e) {
          errorMessage = `HTTP ${response.status}: ${response.statusText}`
        }
        throw new Error(errorMessage)
      }

      showNotification('Provider created successfully!', 'success')
      closeModal()
      fetchProviders()
    } catch (error) {
      console.error('Provider creation error:', error)
      showNotification(error.message || 'Failed to create provider', 'error')
    }
  }

  const handleUpdate = async () => {
    try {
      const response = await fetch(`${API_BASE}/providers/${selectedProvider.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newProvider)
      })

      if (!response.ok) throw new Error('Failed to update provider')

      showNotification('Provider updated successfully!', 'success')
      closeModal()
      fetchProviders()
    } catch (error) {
      showNotification(error.message, 'error')
    }
  }

  const handleDelete = async (id) => {
    if (!confirm('Are you sure you want to delete this provider?')) return

    try {
      const response = await fetch(`${API_BASE}/providers/${id}`, {
        method: 'DELETE'
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.error?.message || 'Failed to delete provider')
      }

      showNotification('Provider deleted successfully!', 'success')
      fetchProviders()
    } catch (error) {
      showNotification(error.message, 'error')
    }
  }

  const resetForm = () => {
    setNewProvider({
      name: '',
      region: '',
      availability_zone: '',
      cloud_provider: 'AWS',
      cpu_total_cores: 100,
      memory_total_gb: 200,
      storage_total_gb: 1000,
      cpu_reservation_percent: 80,
      memory_reservation_percent: 80,
      storage_reservation_percent: 80,
      kubeconfig_content: '',
      api_endpoint: '',
      verify_ssl: true,
      is_active: true,
      priority: 100,
      tags: {},
      metadata: {}
    })
  }

  const closeModal = () => {
    setShowCreateModal(false)
    setSelectedProvider(null)
    resetForm()
  }

  const openEditModal = (provider) => {
    setSelectedProvider(provider)
    setNewProvider({
      name: provider.name,
      region: provider.region,
      availability_zone: provider.availability_zone || '',
      cloud_provider: provider.cloud_provider || 'AWS',
      cpu_total_cores: provider.resources.cpu.total_cores,
      memory_total_gb: provider.resources.memory.total_gb,
      storage_total_gb: provider.resources.storage.total_gb,
      cpu_reservation_percent: provider.resources.cpu.reservation_percent,
      memory_reservation_percent: provider.resources.memory.reservation_percent,
      storage_reservation_percent: provider.resources.storage.reservation_percent,
      kubeconfig_content: provider.kubeconfig_content || '',
      api_endpoint: provider.api_endpoint || '',
      verify_ssl: provider.verify_ssl !== undefined ? provider.verify_ssl : true,
      is_active: provider.is_active,
      priority: provider.priority,
      tags: provider.tags || {},
      metadata: provider.metadata || {}
    })
    setShowCreateModal(true)
  }

  const calculateUsagePercent = (allocated, total) => {
    if (total === 0) return 0
    return ((allocated / total) * 100).toFixed(1)
  }

  const getStatusColor = (isActive) => {
    return isActive ? '#10b981' : '#ef4444'
  }

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner"></div>
        <p>Loading providers...</p>
        <p style={{fontSize: '0.8rem', marginTop: '1rem'}}>Check browser console (F12) for details</p>
      </div>
    )
  }

  return (
    <div className="providers-page">
      {/* Header */}
      <div className="top-bar">
        <div>
          <h2>Provider Clusters</h2>
          <p className="subtitle">Manage Kubernetes clusters for database deployments</p>
        </div>
        <button className="btn-primary" onClick={() => { resetForm(); setShowCreateModal(true) }}>
          + New Provider
        </button>
      </div>

      {/* Content */}
      <div className="content-area">
        {/* Debug info */}
        <div style={{padding: '0.5rem', background: '#f3f4f6', marginBottom: '1rem', fontSize: '0.8rem'}}>
          <strong>Debug:</strong> Providers count: {providers.length} | Loading: {loading ? 'Yes' : 'No'}
        </div>
        
        {providers.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">📦</div>
            <h3>No providers configured</h3>
            <p>Get started by adding your first Kubernetes cluster</p>
            <p style={{fontSize: '0.8rem', color: '#666', marginTop: '0.5rem'}}>
              Check browser console (F12) for API response details
            </p>
            <button className="btn-primary" onClick={() => { resetForm(); setShowCreateModal(true) }}>
              + New Provider
            </button>
          </div>
        ) : (
          <div className="providers-grid">
            {providers.map(provider => (
              <div key={provider.id} className="provider-card">
                <div className="provider-header">
                  <div>
                    <h3>{provider.name}</h3>
                    <p className="provider-location">
                      {provider.cloud_provider} • {provider.region}
                      {provider.availability_zone && ` • ${provider.availability_zone}`}
                    </p>
                  </div>
                  <span
                    className="status-badge"
                    style={{ backgroundColor: getStatusColor(provider.is_active) }}
                  >
                    {provider.is_active ? 'Active' : 'Inactive'}
                  </span>
                </div>

                <div className="provider-body">
                  <div className="resource-row">
                    <span className="resource-label">CPU</span>
                    <span className="resource-value">
                      {provider.resources.cpu.allocated_cores.toFixed(1)} / {provider.resources.cpu.total_cores} cores
                    </span>
                  </div>
                  <div className="progress-bar">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${calculateUsagePercent(provider.resources.cpu.allocated_cores, provider.resources.cpu.total_cores)}%`,
                        background: '#2563eb'
                      }}
                    ></div>
                  </div>

                  <div className="resource-row">
                    <span className="resource-label">Memory</span>
                    <span className="resource-value">
                      {provider.resources.memory.allocated_gb.toFixed(1)} / {provider.resources.memory.total_gb} GB
                    </span>
                  </div>
                  <div className="progress-bar">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${calculateUsagePercent(provider.resources.memory.allocated_gb, provider.resources.memory.total_gb)}%`,
                        background: '#8b5cf6'
                      }}
                    ></div>
                  </div>

                  <div className="resource-row">
                    <span className="resource-label">Storage</span>
                    <span className="resource-value">
                      {provider.resources.storage.allocated_gb.toFixed(1)} / {provider.resources.storage.total_gb} GB
                    </span>
                  </div>
                  <div className="progress-bar">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${calculateUsagePercent(provider.resources.storage.allocated_gb, provider.resources.storage.total_gb)}%`,
                        background: '#10b981'
                      }}
                    ></div>
                  </div>
                </div>

                <div className="provider-footer">
                  <span className="priority-badge">Priority: {provider.priority}</span>
                  <div className="action-buttons">
                    <button className="btn-secondary" onClick={() => openEditModal(provider)}>
                      Edit
                    </button>
                    <button className="btn-icon-danger" onClick={() => handleDelete(provider.id)}>
                      ×
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create/Edit Modal */}
      {showCreateModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal modal-large" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{selectedProvider ? 'Edit Provider' : 'New Provider'}</h2>
              <button className="modal-close" onClick={closeModal}>×</button>
            </div>

            <div className="modal-body">
              {/* Basic Information */}
              <div className="form-section">
                <h3>Basic Information</h3>
                <div className="form-grid">
                  <div className="form-group">
                    <label>
                      Provider Name <span className="required">*</span>
                    </label>
                    <input
                      type="text"
                      value={newProvider.name}
                      onChange={e => setNewProvider({...newProvider, name: e.target.value})}
                      placeholder="prod-cluster-us-east"
                      disabled={!!selectedProvider}
                    />
                    <small>Unique identifier (cannot be changed)</small>
                  </div>

                  <div className="form-group">
                    <label>Cloud Provider</label>
                    <select
                      value={newProvider.cloud_provider}
                      onChange={e => setNewProvider({...newProvider, cloud_provider: e.target.value})}
                    >
                      <option value="AWS">AWS</option>
                      <option value="GCP">Google Cloud</option>
                      <option value="Azure">Azure</option>
                      <option value="DigitalOcean">DigitalOcean</option>
                      <option value="On-Premise">On-Premise</option>
                      <option value="Other">Other</option>
                    </select>
                  </div>

                  <div className="form-group">
                    <label>
                      Region <span className="required">*</span>
                    </label>
                    <input
                      type="text"
                      value={newProvider.region}
                      onChange={e => setNewProvider({...newProvider, region: e.target.value})}
                      placeholder="us-east-1"
                    />
                  </div>

                  <div className="form-group">
                    <label>Availability Zone</label>
                    <input
                      type="text"
                      value={newProvider.availability_zone}
                      onChange={e => setNewProvider({...newProvider, availability_zone: e.target.value})}
                      placeholder="us-east-1a"
                    />
                    <small>Optional</small>
                  </div>
                </div>
              </div>

              {/* Resource Capacity */}
              <div className="form-section">
                <h3>Resource Capacity</h3>
                <div className="form-grid">
                  <div className="form-group">
                    <label>
                      CPU Cores <span className="required">*</span>
                    </label>
                    <input
                      type="number"
                      value={newProvider.cpu_total_cores}
                      onChange={e => setNewProvider({...newProvider, cpu_total_cores: parseFloat(e.target.value)})}
                      min="1"
                      step="0.5"
                    />
                  </div>

                  <div className="form-group">
                    <label>
                      Memory (GB) <span className="required">*</span>
                    </label>
                    <input
                      type="number"
                      value={newProvider.memory_total_gb}
                      onChange={e => setNewProvider({...newProvider, memory_total_gb: parseFloat(e.target.value)})}
                      min="1"
                    />
                  </div>

                  <div className="form-group">
                    <label>
                      Storage (GB) <span className="required">*</span>
                    </label>
                    <input
                      type="number"
                      value={newProvider.storage_total_gb}
                      onChange={e => setNewProvider({...newProvider, storage_total_gb: parseFloat(e.target.value)})}
                      min="1"
                      step="10"
                    />
                  </div>
                </div>

                <h4>Reservation Limits (%)</h4>
                <small>Maximum percentage of resources that can be allocated</small>
                <div className="form-grid" style={{ marginTop: '1rem' }}>
                  <div className="form-group">
                    <label>CPU Reservation: {newProvider.cpu_reservation_percent}%</label>
                    <input
                      type="range"
                      className="slider"
                      value={newProvider.cpu_reservation_percent}
                      onChange={e => setNewProvider({...newProvider, cpu_reservation_percent: parseInt(e.target.value)})}
                      min="0"
                      max="100"
                      step="5"
                    />
                    <small>{((newProvider.cpu_total_cores * newProvider.cpu_reservation_percent) / 100).toFixed(1)} cores allocatable</small>
                  </div>

                  <div className="form-group">
                    <label>Memory Reservation: {newProvider.memory_reservation_percent}%</label>
                    <input
                      type="range"
                      className="slider"
                      value={newProvider.memory_reservation_percent}
                      onChange={e => setNewProvider({...newProvider, memory_reservation_percent: parseInt(e.target.value)})}
                      min="0"
                      max="100"
                      step="5"
                    />
                    <small>{((newProvider.memory_total_gb * newProvider.memory_reservation_percent) / 100).toFixed(1)} GB allocatable</small>
                  </div>

                  <div className="form-group">
                    <label>Storage Reservation: {newProvider.storage_reservation_percent}%</label>
                    <input
                      type="range"
                      className="slider"
                      value={newProvider.storage_reservation_percent}
                      onChange={e => setNewProvider({...newProvider, storage_reservation_percent: parseInt(e.target.value)})}
                      min="0"
                      max="100"
                      step="5"
                    />
                    <small>{((newProvider.storage_total_gb * newProvider.storage_reservation_percent) / 100).toFixed(1)} GB allocatable</small>
                  </div>
                </div>
              </div>

              {/* Kubernetes Configuration */}
              <div className="form-section">
                <h3>Kubernetes Configuration</h3>
                <div className="form-group">
                  <label>
                    Kubeconfig <span className="required">*</span>
                  </label>
                  <div style={{ marginBottom: '0.5rem' }}>
                    <input
                      type="file"
                      id="kubeconfig-upload"
                      accept=".yaml,.yml,.txt"
                      onChange={handleFileUpload}
                      style={{ display: 'none' }}
                    />
                    <label htmlFor="kubeconfig-upload" className="btn-secondary" style={{ cursor: 'pointer' }}>
                      Choose File
                    </label>
                    <small style={{ marginLeft: '1rem' }}>Or paste content below</small>
                  </div>
                  <textarea
                    value={newProvider.kubeconfig_content}
                    onChange={e => setNewProvider({...newProvider, kubeconfig_content: e.target.value})}
                    placeholder="Paste kubeconfig content here (YAML format)..."
                    rows="8"
                    style={{ fontFamily: 'monospace', fontSize: '0.875rem' }}
                  />
                  <small>Provide kubeconfig to connect to your Kubernetes cluster</small>
                </div>

                <div className="form-group">
                  <label>API Endpoint</label>
                  <input
                    type="text"
                    value={newProvider.api_endpoint}
                    onChange={e => setNewProvider({...newProvider, api_endpoint: e.target.value})}
                    placeholder="https://cluster.example.com:6443"
                  />
                  <small>Optional - extracted from kubeconfig if not provided</small>
                </div>

                <div className="form-group">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={newProvider.verify_ssl}
                      onChange={e => setNewProvider({...newProvider, verify_ssl: e.target.checked})}
                    />
                    Verify SSL Certificates
                  </label>
                  <small style={{ display: 'block', marginTop: '0.5rem', marginLeft: '1.5rem' }}>
                    Disable only for clusters with certificate issues (not recommended for production)
                  </small>
                </div>
              </div>

              {/* Settings */}
              <div className="form-section">
                <h3>Settings</h3>
                <div className="form-grid">
                  <div className="form-group">
                    <label>Priority</label>
                    <input
                      type="number"
                      value={newProvider.priority}
                      onChange={e => setNewProvider({...newProvider, priority: parseInt(e.target.value)})}
                      min="0"
                      max="1000"
                    />
                    <small>Higher priority selected first (0-1000)</small>
                  </div>

                  <div className="form-group">
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={newProvider.is_active}
                        onChange={e => setNewProvider({...newProvider, is_active: e.target.checked})}
                      />
                      Active (can host new databases)
                    </label>
                  </div>
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <button className="btn-secondary" onClick={closeModal}>
                Cancel
              </button>
              <button
                className="btn-primary"
                onClick={selectedProvider ? handleUpdate : handleCreate}
              >
                {selectedProvider ? 'Update Provider' : 'Create Provider'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default Providers
