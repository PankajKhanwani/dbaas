import { useState, useEffect } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8003/api/v1'
const DOMAIN = 'demo'
const PROJECT = 'demo'

function App() {
  const [databases, setDatabases] = useState([])
  const [versions, setVersions] = useState({})
  const [loading, setLoading] = useState(true)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [selectedDb, setSelectedDb] = useState(null)
  const [showDetailsModal, setShowDetailsModal] = useState(false)
  const [dbStatus, setDbStatus] = useState(null)
  const [dbCredentials, setDbCredentials] = useState(null)
  const [dbMetrics, setDbMetrics] = useState(null)
  const [metricsLoading, setMetricsLoading] = useState(false)
  const [dbBackups, setDbBackups] = useState([])
  const [backupsLoading, setBackupsLoading] = useState(false)
  const [activeTab, setActiveTab] = useState('overview')
  const [notification, setNotification] = useState(null)
  const [currentPage, setCurrentPage] = useState('databases') // 'databases', 'providers', 'backups'
  const [view, setView] = useState('grid') // 'grid', 'list' for database view
  const [allBackups, setAllBackups] = useState([])
  const [allBackupsLoading, setAllBackupsLoading] = useState(false)
  const [metricSearch, setMetricSearch] = useState('')
  const [collapsedCategories, setCollapsedCategories] = useState({})
  const [availableUpgrades, setAvailableUpgrades] = useState([])
  const [upgradeLoading, setUpgradeLoading] = useState(false)
  const [showUpgradeModal, setShowUpgradeModal] = useState(false)
  const [selectedUpgradeVersion, setSelectedUpgradeVersion] = useState('')

  const [newDb, setNewDb] = useState({
    name: '',
    region: '',
    availability_zone: '',
    engine: '',
    version: '',
    size: 'db.t3.micro',
    storage_gb: 10,
    replicas: 1,
    backup_enabled: true,
    backup_schedule: 'daily',
    backup_retention_days: 30,
    high_availability: false,
    monitoring_enabled: true,
    username: '',
    password: ''
  })
  const [availableVersions, setAvailableVersions] = useState([])
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [availableEngines, setAvailableEngines] = useState({})
  const [loadingEngines, setLoadingEngines] = useState(false)

  useEffect(() => {
    fetchVersions()
  }, [])

  useEffect(() => {
    fetchDatabases()
    const interval = setInterval(fetchDatabases, 5000)
    return () => clearInterval(interval)
  }, [])

  const showNotification = (message, type = 'success') => {
    setNotification({ message, type })
    setTimeout(() => setNotification(null), 4000)
  }

  const fetchVersions = async () => {
    try {
      const response = await fetch(`${API_BASE}/versions/`)
      const data = await response.json()
      setVersions(data)

      // Set first available engine as default
      const engines = Object.keys(data)
      if (engines.length > 0) {
        setNewDb(prev => ({ ...prev, engine: engines[0] }))
      }
    } catch (error) {
      showNotification('Failed to fetch versions', 'error')
    }
  }

  const fetchEnginesFromProvider = async (region, availabilityZone) => {
    if (!region) return

    setLoadingEngines(true)
    setAvailableEngines({})
    setAvailableVersions([])
    setNewDb(prev => ({ ...prev, engine: '', version: '' }))

    try {
      let url = `${API_BASE}/domain/${DOMAIN}/providers/engines`
      const params = new URLSearchParams()
      if (region) params.append('region', region)
      if (availabilityZone) params.append('availability_zone', availabilityZone)

      if (params.toString()) {
        url += `?${params.toString()}`
      }

      const response = await fetch(url)
      const data = await response.json()

      if (data.error) {
        showNotification(data.error, 'error')
        setAvailableEngines({})
      } else {
        setAvailableEngines(data.engines || {})
        // Auto-select first engine if available
        const engines = Object.keys(data.engines || {})
        if (engines.length > 0) {
          const firstEngine = engines[0]
          const versions = data.engines[firstEngine] || []
          setNewDb(prev => ({
            ...prev,
            engine: firstEngine,
            version: versions.length > 0 ? versions[0].version : ''
          }))
          setAvailableVersions(versions)
        }
      }
    } catch (error) {
      showNotification('Failed to fetch engines from provider', 'error')
      setAvailableEngines({})
    } finally {
      setLoadingEngines(false)
    }
  }

  const fetchVersionsFromProvider = async (engine, region, availabilityZone) => {
    if (!engine) return

    setLoadingVersions(true)
    try {
      let url = `${API_BASE}/providers/versions/${engine}`
      const params = new URLSearchParams()
      if (region) params.append('region', region)
      if (availabilityZone) params.append('availability_zone', availabilityZone)

      if (params.toString()) {
        url += `?${params.toString()}`
      }

      const response = await fetch(url)
      const data = await response.json()

      if (data.error) {
        showNotification(data.error, 'error')
        setAvailableVersions([])
      } else {
        setAvailableVersions(data.versions || [])
        // Auto-select first version if available
        if (data.versions && data.versions.length > 0) {
          setNewDb(prev => ({ ...prev, version: data.versions[0].version }))
        }
      }
    } catch (error) {
      showNotification('Failed to fetch versions from provider', 'error')
      setAvailableVersions([])
    } finally {
      setLoadingVersions(false)
    }
  }

  const fetchDatabases = async () => {
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/`)
      if (!response.ok) {
        console.error('Failed to fetch databases:', response.status, response.statusText)
        setLoading(false)
        return
      }
      const data = await response.json()
      console.log('Fetched databases:', data.databases?.length || 0, 'databases')
      setDatabases(data.databases || [])
      setLoading(false)
    } catch (error) {
      console.error('Error fetching databases:', error)
      setLoading(false)
    }
  }

  const fetchDbDetails = async (id) => {
    try {
      const [statusRes, credsRes] = await Promise.all([
        fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/status`),
        fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/credentials`)
      ])
      const status = await statusRes.json()
      const creds = credsRes.ok ? await credsRes.json() : null
      setDbStatus(status)
      setDbCredentials(creds)
    } catch (error) {
      console.error('Failed to fetch details:', error)
    }
  }

  const fetchDbMetrics = async (id) => {
    setMetricsLoading(true)
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/metrics`)
      if (response.ok) {
        const metrics = await response.json()
        setDbMetrics(metrics)
      } else {
        setDbMetrics(null)
        showNotification('Failed to fetch metrics. Make sure monitoring is enabled.', 'error')
      }
    } catch (error) {
      console.error('Failed to fetch metrics:', error)
      setDbMetrics(null)
      showNotification('Error fetching metrics', 'error')
    } finally {
      setMetricsLoading(false)
    }
  }

  const openDetails = async (db) => {
    setSelectedDb(db)
    setShowDetailsModal(true)
    setActiveTab('overview')
    setDbMetrics(null)
    setDbBackups([])
    await fetchDbDetails(db.id)
    if (db.backup_enabled) {
      await fetchDbBackups(db.id)
    }
  }

  const createDatabase = async (e) => {
    e.preventDefault()
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newDb)
      })
      if (response.ok) {
        showNotification('Database created successfully!')
        setShowCreateModal(false)
        setNewDb({
          name: '', engine: 'mongodb', version: '', size: 'db.t3.micro',
          storage_gb: 10, replicas: 1, backup_enabled: true, backup_schedule: 'daily',
          backup_retention_days: 30, high_availability: false, monitoring_enabled: true
        })
        fetchDatabases()
      } else {
        const error = await response.json()
        showNotification(error.detail || 'Failed to create database', 'error')
      }
    } catch (error) {
      showNotification('Failed to create database', 'error')
    }
  }

  const deleteDatabase = async (id, name) => {
    if (!confirm(`Delete database "${name}"? This cannot be undone.`)) return
    try {
      await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}`, {
        method: 'DELETE'
      })
      showNotification('Database deleted successfully!')
      setShowDetailsModal(false)
      fetchDatabases()
    } catch (error) {
      showNotification('Failed to delete database', 'error')
    }
  }

  const scaleDatabase = async (id, newReplicas) => {
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/scale`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replicas: newReplicas })
      })
      if (response.ok) {
        showNotification('Scaling initiated!')
        fetchDatabases()
      }
    } catch (error) {
      showNotification('Failed to scale database', 'error')
    }
  }

  const updateDatabaseConfig = async (id, updates) => {
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
      })
      if (response.ok) {
        showNotification('Database configuration updated!')
        fetchDatabases()
        setShowDetailsModal(false)
      } else {
        const error = await response.json()
        showNotification(error.detail || 'Failed to update database', 'error')
      }
    } catch (error) {
      showNotification('Failed to update database', 'error')
    }
  }

  const pauseDatabase = async (id) => {
    try {
      await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/pause`, {
        method: 'POST'
      })
      showNotification('Database paused!')
      fetchDatabases()
    } catch (error) {
      showNotification('Failed to pause database', 'error')
    }
  }

  const resumeDatabase = async (id) => {
    try {
      await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/resume`, {
        method: 'POST'
      })
      showNotification('Database resumed!')
      fetchDatabases()
    } catch (error) {
      showNotification('Failed to resume database', 'error')
    }
  }

  // Helper function to determine upgrade type
  const getUpgradeType = (currentVersion, targetVersion) => {
    const parseCurrent = currentVersion.split('.').map(v => parseInt(v.replace(/\D/g, '')) || 0)
    const parseTarget = targetVersion.split('.').map(v => parseInt(v.replace(/\D/g, '')) || 0)

    if (parseCurrent[0] !== parseTarget[0]) return 'MAJOR'
    if (parseCurrent[1] !== parseTarget[1]) return 'MINOR'
    return 'PATCH'
  }

  const fetchAvailableUpgrades = async (id) => {
    setUpgradeLoading(true)
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/available-upgrades`)
      const data = await response.json()
      setAvailableUpgrades(data.available_upgrades || [])
      return data
    } catch (error) {
      showNotification('Failed to fetch available upgrades', 'error')
      setAvailableUpgrades([])
    } finally {
      setUpgradeLoading(false)
    }
  }

  const upgradeDatabase = async (id, targetVersion) => {
    if (!targetVersion) {
      showNotification('Please select a version to upgrade to', 'error')
      return
    }

    if (!confirm(`Are you sure you want to upgrade to version ${targetVersion}? This operation cannot be undone and may cause temporary downtime.`)) {
      return
    }

    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/upgrade`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_version: targetVersion,
          skip_backup: false,
          apply_immediately: true
        })
      })

      const data = await response.json()

      if (data.error) {
        showNotification(`Upgrade failed: ${data.error}`, 'error')
      } else {
        showNotification(`Database upgrade to ${targetVersion} initiated!`)
        setShowUpgradeModal(false)
        setSelectedUpgradeVersion('')
        fetchDatabases()
      }
    } catch (error) {
      showNotification('Failed to initiate upgrade', 'error')
    }
  }

  const getStatusColor = (status) => {
    const colors = {
      pending: '#f59e0b', provisioning: '#3b82f6', running: '#10b981',
      failed: '#ef4444', scaling: '#8b5cf6', paused: '#6b7280'
    }
    return colors[status] || '#9ca3af'
  }

  const getStatusIcon = (status) => {
    const icons = {
      pending: '⏳', provisioning: '🔄', running: '✅',
      failed: '❌', scaling: '📈', paused: '⏸️'
    }
    return icons[status] || '•'
  }

  const getEngineIcon = (engine) => {
    const icons = {
      mongodb: '🍃',
      postgres: '🐘',
      mysql: '🐬',
      mariadb: '🦭',
      redis: '🔴',
      elasticsearch: '🔍'
    }
    return icons[engine] || '🗄️'
  }

  const fetchDbBackups = async (id) => {
    setBackupsLoading(true)
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/backups`)
      if (response.ok) {
        const data = await response.json()
        setDbBackups(data.backups || [])
      } else {
        setDbBackups([])
      }
    } catch (error) {
      console.error('Failed to fetch backups:', error)
      setDbBackups([])
    } finally {
      setBackupsLoading(false)
    }
  }

  const triggerBackup = async (id) => {
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/backup`, {
        method: 'POST'
      })
      if (response.ok) {
        showNotification('Backup triggered successfully!', 'success')
        await fetchDbBackups(id)
        if (currentPage === 'backups') {
          await fetchAllBackups()
        }
      } else {
        const error = await response.json()
        showNotification(error.error?.message || 'Failed to trigger backup', 'error')
      }
    } catch (error) {
      showNotification('Failed to trigger backup', 'error')
    }
  }

  const restoreDatabase = async (id, backupId) => {
    if (!confirm(`Restore database from backup "${backupId}"? This will overwrite current data.`)) return
    try {
      const response = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${id}/restore?backup_id=${backupId}`, {
        method: 'POST'
      })
      if (response.ok) {
        showNotification('Restore initiated successfully!', 'success')
        await fetchDbBackups(id)
        if (currentPage === 'backups') {
          await fetchAllBackups()
        }
      } else {
        const error = await response.json()
        showNotification(error.error?.message || 'Failed to restore database', 'error')
      }
    } catch (error) {
      showNotification('Failed to restore database', 'error')
    }
  }

  const fetchAllBackups = async () => {
    setAllBackupsLoading(true)
    try {
      // Fetch all databases with backup enabled
      const dbResponse = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/`)
      if (!dbResponse.ok) {
        throw new Error('Failed to fetch databases')
      }
      const dbData = await dbResponse.json()
      const backupEnabledDbs = (dbData.databases || []).filter(db => db.backup_enabled && db.status === 'running')
      
      // Fetch backups for each database
      const backupPromises = backupEnabledDbs.map(async (db) => {
        try {
          const backupResponse = await fetch(`${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${db.id}/backups`)
          if (backupResponse.ok) {
            const backupData = await backupResponse.json()
            return (backupData.backups || []).map(backup => ({
              ...backup,
              database_id: db.id,
              database_name: db.name,
              database_engine: db.engine
            }))
          }
          return []
        } catch (error) {
          console.error(`Failed to fetch backups for ${db.name}:`, error)
          return []
        }
      })
      
      const allBackupsArrays = await Promise.all(backupPromises)
      const flattenedBackups = allBackupsArrays.flat()
      
      // Sort by created_at (newest first)
      flattenedBackups.sort((a, b) => {
        const dateA = new Date(a.created_at || 0)
        const dateB = new Date(b.created_at || 0)
        return dateB - dateA
      })
      
      setAllBackups(flattenedBackups)
    } catch (error) {
      console.error('Failed to fetch all backups:', error)
      setAllBackups([])
      showNotification('Failed to fetch backups', 'error')
    } finally {
      setAllBackupsLoading(false)
    }
  }

  const getDefaultUsername = (engine) => {
    const defaults = {
      mariadb: 'root',
      elasticsearch: 'elastic'
    }
    return defaults[engine] || ''
  }

  const isUsernameFixed = (engine) => {
    return engine === 'mariadb' || engine === 'elasticsearch'
  }

  return (
    <div className="app">
      {/* Sidebar */}
      <div className="sidebar">
        <div className="sidebar-header">
          <div className="logo-container">
            <div className="logo">⚡</div>
            <div>
              <h1>KubeDB Platform</h1>
              <p className="sidebar-subtitle">Enterprise DBaaS</p>
            </div>
          </div>
        </div>
        <nav className="sidebar-nav">
          <button 
            className={currentPage === 'databases' ? 'nav-item active' : 'nav-item'}
            onClick={() => setCurrentPage('databases')}
          >
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/>
            </svg>
            <span>Databases</span>
            <span className="nav-badge">{databases.length}</span>
          </button>
          <button className="nav-item disabled">
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
            </svg>
            <span>Monitoring</span>
          </button>
          <button 
            className={currentPage === 'backups' ? 'nav-item active' : 'nav-item'}
            onClick={() => {
              setCurrentPage('backups')
              fetchAllBackups()
            }}
          >
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
            </svg>
            <span>Backups</span>
          </button>
          <button className="nav-item disabled">
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/>
            </svg>
            <span>Settings</span>
          </button>
        </nav>
        <div className="sidebar-footer">
          <div className="stats-box-enhanced">
            <div className="stat-item-enhanced">
              <div className="stat-icon-circle blue">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                </svg>
              </div>
              <div>
                <span className="stat-label">Total Databases</span>
                <span className="stat-value-enhanced">{databases.length}</span>
              </div>
            </div>
            <div className="stat-item-enhanced">
              <div className="stat-icon-circle green">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
                </svg>
              </div>
              <div>
                <span className="stat-label">Active Now</span>
                <span className="stat-value-enhanced running">{databases.filter(d => d.status === 'running').length}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        <header className="top-bar">
          <div>
            <h2>Database Instances</h2>
            <p className="subtitle">Manage your database deployments</p>
          </div>
          <div className="top-bar-actions">
            <div className="view-toggle">
              <button
                className={view === 'grid' ? 'active' : ''}
                onClick={() => setView('grid')}
              >
                ▦ Grid
              </button>
              <button
                className={view === 'list' ? 'active' : ''}
                onClick={() => setView('list')}
              >
                ☰ List
              </button>
            </div>
            <button className="btn-primary" onClick={() => setShowCreateModal(true)}>
              + New Database
            </button>
          </div>
        </header>

        {/* Notification Toast */}
        {notification && (
          <div className={`notification notification-${notification.type}`}>
            {notification.message}
          </div>
        )}

        {/* Backups Page */}
        {currentPage === 'backups' && (
          <div className="backups-page">
            <div style={{marginBottom: '2rem'}}>
              <h2>💾 All Backups</h2>
              <p className="subtitle">View and manage backups from all databases</p>
            </div>

            {allBackupsLoading ? (
              <div className="loading">
                <div className="spinner"></div>
                <p>Loading backups...</p>
              </div>
            ) : allBackups.length === 0 ? (
              <div className="empty-state">
                <div className="empty-icon">💾</div>
                <h3>No backups found</h3>
                <p>Backups will appear here once databases are backed up</p>
              </div>
            ) : (
              <div className="backups-list-page">
                <div style={{marginBottom: '1rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center'}}>
                  <div>
                    <strong>Total Backups: {allBackups.length}</strong>
                    <span style={{marginLeft: '1rem', color: '#6b7280', fontSize: '0.9rem'}}>
                      Successful: {allBackups.filter(b => b.phase === 'Succeeded' || b.phase === 'Complete').length}
                    </span>
                  </div>
                  <button 
                    className="btn-secondary"
                    onClick={fetchAllBackups}
                    disabled={allBackupsLoading}
                  >
                    🔄 Refresh
                  </button>
                </div>

                {allBackups.map((backup, idx) => {
                  const getStatusColor = (phase) => {
                    if (phase === 'Succeeded' || phase === 'Complete') return '#10b981'
                    if (phase === 'Failed') return '#ef4444'
                    if (phase === 'Running') return '#3b82f6'
                    return '#6b7280'
                  }
                  
                  const getStatusIcon = (phase) => {
                    if (phase === 'Succeeded' || phase === 'Complete') return '✅'
                    if (phase === 'Failed') return '❌'
                    if (phase === 'Running') return '🔄'
                    return '⏳'
                  }

                  let backupDate = backup.created_at || backup.backup_id
                  if (backup.backup_id && backup.backup_id.includes('-')) {
                    const parts = backup.backup_id.split('-')
                    const timestamp = parts[parts.length - 1]
                    if (timestamp && timestamp.length === 15) {
                      const dateStr = timestamp.substring(0, 8)
                      const timeStr = timestamp.substring(9, 15)
                      backupDate = `${dateStr} ${timeStr.substring(0, 2)}:${timeStr.substring(2, 4)}:${timeStr.substring(4, 6)}`
                    }
                  }

                  // Find database info
                  const db = databases.find(d => d.id === backup.database_id)

                  return (
                    <div key={idx} className="backup-item-card" style={{
                      border: '1px solid #e5e7eb',
                      borderRadius: '12px',
                      padding: '1.5rem',
                      marginBottom: '1rem',
                      background: 'white',
                      boxShadow: '0 1px 3px rgba(0,0,0,0.1)'
                    }}>
                      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start'}}>
                        <div style={{flex: 1}}>
                          <div style={{display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem'}}>
                            <span style={{fontSize: '1.5rem'}}>{getStatusIcon(backup.phase)}</span>
                            <div>
                              <h4 style={{margin: 0, fontSize: '1.1rem'}}>{backup.backup_id}</h4>
                              <p style={{margin: '0.25rem 0 0 0', color: '#6b7280', fontSize: '0.9rem'}}>
                                Database: <strong>{backup.database_name || 'Unknown'}</strong> ({backup.database_engine || 'unknown'})
                              </p>
                            </div>
                            <span style={{
                              backgroundColor: getStatusColor(backup.phase),
                              color: 'white',
                              padding: '0.35rem 0.75rem',
                              borderRadius: '6px',
                              fontSize: '0.85rem',
                              fontWeight: 'bold'
                            }}>
                              {backup.phase}
                            </span>
                          </div>
                          <div style={{fontSize: '0.85rem', color: '#6b7280', marginTop: '0.5rem'}}>
                            <div>Created: {backupDate}</div>
                            {backup.type && <div>Type: {backup.type}</div>}
                            {db && (
                              <div style={{marginTop: '0.5rem'}}>
                                <button
                                  className="btn-secondary"
                                  onClick={() => {
                                    setCurrentPage('databases')
                                    setTimeout(() => openDetails(db), 100)
                                  }}
                                  style={{padding: '0.4rem 0.8rem', fontSize: '0.85rem'}}
                                >
                                  View Database
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                        <div>
                          {(backup.phase === 'Succeeded' || backup.phase === 'Complete') && backup.database_id && (
                            <button
                              className="btn-primary"
                              onClick={() => {
                                let restoreId = backup.backup_id
                                if (backup.backup_id.includes('backup-')) {
                                  restoreId = backup.backup_id.split('backup-')[1]
                                }
                                restoreDatabase(backup.database_id, restoreId)
                              }}
                              style={{padding: '0.5rem 1rem'}}
                            >
                              🔄 Restore
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* Database Grid/List */}
        {currentPage === 'databases' && (
          <>
        {loading ? (
          <div className="loading">
            <div className="spinner"></div>
            <p>Loading databases...</p>
          </div>
        ) : databases.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">🗄️</div>
            <h3>No databases yet</h3>
            <p>Create your first database to get started</p>
            <button className="btn-primary" onClick={() => setShowCreateModal(true)}>
              + Create Database
            </button>
          </div>
        ) : (
          <div className={`db-${view}`}>
            {databases.map(db => (
              <div key={db.id} className="db-card" onClick={() => openDetails(db)}>
                <div className="db-card-header">
                  <div>
                    <h3>
                      <span className="engine-icon">{getEngineIcon(db.engine)}</span>
                      {db.name}
                    </h3>
                    <p className="db-meta">
                      {db.engine}
                      {db.engine === 'redis' && db.version && db.version.includes('valkey') && (
                        <span style={{color: '#8b5cf6', fontWeight: 600}}> (Valkey)</span>
                      )}
                      {' '}{db.version}
                    </p>
                  </div>
                  <span
                    className="status-badge"
                    style={{ backgroundColor: getStatusColor(db.status) }}
                  >
                    {getStatusIcon(db.status)} {db.status}
                  </span>
                </div>
                <div className="db-card-body">
                  <div className="info-grid">
                    <div className="info-item">
                      <span className="info-label">Replicas</span>
                      <span className="info-value">
                        {db.ready_replicas}/{db.replicas}
                        {db.ready_replicas < db.replicas && ' ⚠️'}
                      </span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">Storage</span>
                      <span className="info-value">{db.storage_gb} GB</span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">Backup</span>
                      <span className="info-value">{db.backup_enabled ? '✅' : '❌'}</span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">HA</span>
                      <span className="info-value">{db.high_availability ? '✅' : '❌'}</span>
                    </div>
                  </div>
                  {db.endpoint && (
                    <div className="endpoint-box">
                      <code>{db.endpoint}:{db.port}</code>
                    </div>
                  )}
                </div>
                <div className="db-card-footer">
                  <span className="timestamp">Created {new Date(db.created_at).toLocaleDateString()}</span>
                  <div className="card-actions">
                    <button
                      className="btn-icon btn-icon-danger"
                      onClick={(e) => { e.stopPropagation(); deleteDatabase(db.id, db.name); }}
                      title="Delete database"
                    >
                      🗑️
                    </button>
                    <button
                      className="btn-icon"
                      onClick={(e) => { e.stopPropagation(); openDetails(db); }}
                    >
                      View Details →
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
          </>
        )}
      </div>

      {/* Create Database Modal - Improved */}
      {showCreateModal && (
        <div className="modal-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="modal modal-create" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h2>🚀 Create Database</h2>
                <p className="modal-subtitle">Deploy a new managed database instance</p>
              </div>
              <button className="modal-close" onClick={() => setShowCreateModal(false)}>×</button>
            </div>
            <form onSubmit={createDatabase}>
              <div className="modal-body">
                {/* Quick Start Section */}
                <div className="create-section highlighted">
                  <div className="section-icon">⚙️</div>
                  <div className="section-content">
                    <h3>Essential Configuration</h3>
                    <div className="form-row-vertical">
                      <div className="form-group-inline">
                        <label>Database Name <span className="required">*</span></label>
                        <input
                          type="text"
                          value={newDb.name}
                          onChange={(e) => setNewDb({ ...newDb, name: e.target.value })}
                          placeholder="e.g. my-app-db"
                          className="input-large"
                          required
                        />
                      </div>

                      {/* Database Credentials */}
                      <div className="form-row">
                        <div className="form-group-inline">
                          <label>
                            Username{' '}
                            {isUsernameFixed(newDb.engine) ? (
                              <span className="required">(fixed - cannot be changed)</span>
                            ) : (
                              <span className="optional">(optional - auto-generated if empty)</span>
                            )}
                          </label>
                          <input
                            type="text"
                            value={isUsernameFixed(newDb.engine) ? getDefaultUsername(newDb.engine) : newDb.username}
                            onChange={(e) => setNewDb({ ...newDb, username: e.target.value })}
                            placeholder={isUsernameFixed(newDb.engine)
                              ? getDefaultUsername(newDb.engine)
                              : "Enter custom username or leave empty"}
                            className="input-large"
                            disabled={isUsernameFixed(newDb.engine)}
                            style={isUsernameFixed(newDb.engine) ? {backgroundColor: '#f5f5f5', cursor: 'not-allowed'} : {}}
                          />
                        </div>

                        <div className="form-group-inline">
                          <label>Password <span className="optional">(optional - auto-generated if empty)</span></label>
                          <input
                            type="password"
                            value={newDb.password}
                            onChange={(e) => setNewDb({ ...newDb, password: e.target.value })}
                            placeholder="Enter custom password or leave empty"
                            className="input-large"
                          />
                        </div>
                      </div>

                      <div className="form-row">
                        <div className="form-group-inline">
                          <label>Region <span className="required">*</span></label>
                          <input
                            type="text"
                            value={newDb.region}
                            onChange={(e) => {
                              const region = e.target.value
                              setNewDb({ ...newDb, region, engine: '', version: '' })
                              setAvailableVersions([])
                              setAvailableEngines({})
                              // Fetch engines only when both region AND AZ are provided
                              if (region && newDb.availability_zone) {
                                fetchEnginesFromProvider(region, newDb.availability_zone)
                              }
                            }}
                            placeholder="e.g., us-east-1"
                            className="input-large"
                            required
                          />
                          <small>Cloud region where database will be deployed</small>
                        </div>

                        <div className="form-group-inline">
                          <label>Availability Zone <span className="required">*</span></label>
                          <input
                            type="text"
                            value={newDb.availability_zone}
                            onChange={(e) => {
                              const az = e.target.value
                              setNewDb({ ...newDb, availability_zone: az, engine: '', version: '' })
                              setAvailableVersions([])
                              setAvailableEngines({})
                              // Fetch engines when AZ changes (only if region is also set)
                              if (newDb.region && az) {
                                fetchEnginesFromProvider(newDb.region, az)
                              }
                            }}
                            placeholder="e.g., AZ1"
                            className="input-large"
                            required
                          />
                          <small>Specific AZ for deployment</small>
                        </div>
                      </div>

                      <div className="form-row">
                        <div className="form-group-inline">
                          <label>Engine <span className="required">*</span></label>
                          <select
                            value={newDb.engine}
                            onChange={(e) => {
                              const engine = e.target.value
                              // Get versions for selected engine from availableEngines
                              const versions = availableEngines[engine] || []
                              setAvailableVersions(versions)
                              // MongoDB requires minimum 2 replicas
                              const replicas = (engine === 'mongodb' && newDb.replicas < 2) ? 2 : newDb.replicas
                              setNewDb({
                                ...newDb,
                                engine,
                                version: versions.length > 0 ? versions[0].version : '',
                                replicas: replicas
                              })
                            }}
                            className="select-large"
                            required
                            disabled={!newDb.region || loadingEngines}
                          >
                            <option value="">
                              {loadingEngines
                                ? 'Loading engines...'
                                : !newDb.region
                                ? 'Select region first'
                                : Object.keys(availableEngines).length === 0
                                ? 'No engines available'
                                : 'Select database engine'}
                            </option>
                            {Object.keys(availableEngines).map(engine => (
                              <option key={engine} value={engine}>
                                {engine === 'mongodb' && '🍃 MongoDB'}
                                {engine === 'postgres' && '🐘 PostgreSQL'}
                                {engine === 'mysql' && '🐬 MySQL'}
                                {engine === 'mariadb' && '🦭 MariaDB'}
                                {engine === 'redis' && '🔴 Redis / Valkey'}
                                {engine === 'elasticsearch' && '🔍 Elasticsearch'}
                              </option>
                            ))}
                          </select>
                          {!newDb.region && <small style={{color: '#ef4444'}}>Please select a region first</small>}
                          {loadingEngines && <small style={{color: '#64748b'}}>Fetching engines from cluster...</small>}
                          {!loadingEngines && newDb.region && Object.keys(availableEngines).length === 0 && (
                            <small style={{color: '#ef4444'}}>No engines found for this region</small>
                          )}
                        </div>

                        <div className="form-group-inline">
                          <label>
                            Version <span className="required">*</span>
                            {newDb.engine === 'redis' && (
                              <span className="version-note"> (includes Valkey)</span>
                            )}
                          </label>
                          <select
                            value={newDb.version}
                            onChange={(e) => setNewDb({ ...newDb, version: e.target.value })}
                            className="select-large"
                            disabled={!newDb.engine || loadingEngines || loadingVersions}
                            required
                          >
                            <option value="">
                              {loadingEngines || loadingVersions
                                ? 'Loading versions...'
                                : !newDb.engine
                                ? 'Select engine first'
                                : availableVersions.length === 0
                                ? 'No versions available'
                                : 'Choose version...'}
                            </option>
                            {availableVersions.map(v => {
                              const isValkey = v.name && v.name.includes('valkey')
                              const displayName = isValkey
                                ? `${v.version} - Valkey (${v.name})`
                                : v.version + (v.name && v.name !== v.version ? ` (${v.name})` : '')
                              return (
                                <option key={v.name || v.version} value={v.name || v.version}>
                                  {isValkey && '🟣 '}{displayName}
                                </option>
                              )
                            })}
                          </select>
                          {(loadingEngines || loadingVersions) && <small style={{color: '#64748b'}}>Fetching versions from cluster...</small>}
                          {!loadingEngines && !loadingVersions && newDb.engine && availableVersions.length === 0 && (
                            <small style={{color: '#ef4444'}}>No versions found for this engine</small>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Capacity Section */}
                <div className="create-section">
                  <div className="section-icon">💪</div>
                  <div className="section-content">
                    <h3>Capacity & Performance</h3>
                    <div className="capacity-selector">
                      <div className="capacity-option">
                        <label>Instance Size</label>
                        <select
                          value={newDb.size}
                          onChange={(e) => setNewDb({ ...newDb, size: e.target.value })}
                          className="select-large"
                        >
                          <option value="db.t3.micro">💻 Micro - 0.5 CPU, 1GB RAM</option>
                          <option value="db.t3.small">📊 Small - 1 CPU, 2GB RAM</option>
                          <option value="db.t3.medium">🚀 Medium - 2 CPU, 4GB RAM</option>
                          <option value="db.t3.large">⚡ Large - 2 CPU, 8GB RAM</option>
                        </select>
                      </div>

                      <div className="capacity-sliders">
                        <div className="slider-group">
                          <label>
                            <span>Replicas <span className="required">*</span></span>
                            {newDb.high_availability && newDb.replicas < 3 && (
                              <span style={{color: '#ef4444', fontSize: '0.75rem', marginLeft: '0.5rem'}}>
                                ⚠️ HA requires minimum 3 replicas
                              </span>
                            )}
                            {newDb.high_availability && newDb.replicas >= 3 && newDb.replicas % 2 === 0 && (
                              <span style={{color: '#ef4444', fontSize: '0.75rem', marginLeft: '0.5rem'}}>
                                ⚠️ HA requires odd number (3, 5, 7, etc.)
                              </span>
                            )}
                            {!newDb.high_availability && newDb.replicas > 1 && newDb.replicas % 2 === 0 && (
                              <span style={{color: '#ef4444', fontSize: '0.75rem', marginLeft: '0.5rem'}}>
                                ⚠️ Use odd numbers (1, 3, 5, 7, etc.) to avoid arbiters
                              </span>
                            )}
                            {newDb.engine === 'mongodb' && newDb.replicas < 2 && (
                              <span style={{color: '#ef4444', fontSize: '0.75rem', marginLeft: '0.5rem'}}>
                                ⚠️ MongoDB requires minimum 2 replicas
                              </span>
                            )}
                          </label>
                          <div style={{display: 'flex', gap: '1rem', alignItems: 'center', marginBottom: '0.5rem'}}>
                            <input
                              type="number"
                              min={newDb.high_availability ? 3 : (newDb.engine === 'mongodb' ? 2 : 1)}
                              max="10"
                              step={newDb.high_availability ? 2 : 2}  // Step by 2 to enforce odd numbers
                              value={(() => {
                                let val = newDb.replicas
                                // MongoDB minimum 2
                                if (newDb.engine === 'mongodb' && val < 2) val = 2
                                // HA minimum 3 and must be odd
                                if (newDb.high_availability) {
                                  if (val < 3) val = 3
                                  if (val % 2 === 0) val = val + 1  // Make odd
                                } else {
                                  // Non-HA: must be odd if > 1
                                  if (val > 1 && val % 2 === 0) val = val + 1
                                }
                                return Math.min(10, val)
                              })()}
                              onChange={(e) => {
                                let replicas = parseInt(e.target.value) || 1
                                
                                // MongoDB minimum 2
                                if (newDb.engine === 'mongodb' && replicas < 2) replicas = 2
                                
                                // HA: minimum 3 and must be odd
                                if (newDb.high_availability) {
                                  if (replicas < 3) replicas = 3
                                  if (replicas % 2 === 0) replicas = replicas + 1  // Make odd
                                } else {
                                  // Non-HA: must be odd if > 1
                                  if (replicas > 1 && replicas % 2 === 0) replicas = replicas + 1
                                }
                                
                                setNewDb({ ...newDb, replicas: Math.min(10, Math.max(1, replicas)) })
                              }}
                              style={{
                                width: '80px',
                                padding: '0.5rem',
                                borderRadius: '6px',
                                border: '1px solid #d1d5db',
                                fontSize: '1rem',
                                textAlign: 'center'
                              }}
                              required
                            />
                            <span style={{fontSize: '0.875rem', color: '#6b7280'}}>
                              {newDb.high_availability 
                                ? '(HA: 3, 5, 7, 9)' 
                                : newDb.engine === 'mongodb' 
                                  ? '(MongoDB: 2, 3, 5, 7, 9)'
                                  : '(1, 3, 5, 7, 9)'}
                            </span>
                          </div>
                          <input
                            type="range"
                            min={newDb.high_availability ? 3 : (newDb.engine === 'mongodb' ? 2 : 1)}
                            max="9"
                            step={newDb.high_availability ? 2 : 2}  // Step by 2 for odd numbers
                            value={(() => {
                              let val = newDb.replicas
                              if (newDb.engine === 'mongodb' && val < 2) val = 2
                              if (newDb.high_availability) {
                                if (val < 3) val = 3
                                if (val % 2 === 0) val = val + 1
                              } else {
                                if (val > 1 && val % 2 === 0) val = val + 1
                              }
                              return Math.min(9, val)
                            })()}
                            onChange={(e) => {
                              let replicas = parseInt(e.target.value)
                              
                              if (newDb.engine === 'mongodb' && replicas < 2) replicas = 2
                              
                              if (newDb.high_availability) {
                                if (replicas < 3) replicas = 3
                                if (replicas % 2 === 0) replicas = replicas + 1
                              } else {
                                if (replicas > 1 && replicas % 2 === 0) replicas = replicas + 1
                              }
                              
                              setNewDb({ ...newDb, replicas: Math.min(9, replicas) })
                            }}
                            className="slider"
                          />
                          <div className="slider-labels">
                            <span>{newDb.high_availability ? 3 : (newDb.engine === 'mongodb' ? 2 : 1)}</span>
                            <span>9</span>
                          </div>
                          {newDb.high_availability && (
                            <small style={{color: '#6b7280', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block'}}>
                              High Availability requires minimum 3 replicas (odd numbers: 3, 5, 7, 9)
                            </small>
                          )}
                          {!newDb.high_availability && newDb.engine !== 'mongodb' && (
                            <small style={{color: '#6b7280', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block'}}>
                              Use odd numbers (1, 3, 5, 7, 9) to avoid arbiters
                            </small>
                          )}
                          {newDb.engine === 'mongodb' && !newDb.high_availability && (
                            <small style={{color: '#6b7280', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block'}}>
                              MongoDB: 2 replicas is minimum (will add arbiter). For no arbiter, use 3, 5, 7, 9
                            </small>
                          )}
                        </div>

                        <div className="slider-group">
                          <label>
                            <span>Storage</span>
                            <span className="value-badge">{newDb.storage_gb} GB</span>
                          </label>
                          <input
                            type="range"
                            min="10"
                            max="100"
                            step="10"
                            value={newDb.storage_gb}
                            onChange={(e) => setNewDb({ ...newDb, storage_gb: parseInt(e.target.value) })}
                            className="slider"
                          />
                          <div className="slider-labels">
                            <span>10 GB</span>
                            <span>100 GB</span>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Features Section */}
                <div className="create-section">
                  <div className="section-icon">✨</div>
                  <div className="section-content">
                    <h3>Features</h3>
                    <div className="feature-toggles">
                      <div className={`feature-card ${newDb.backup_enabled ? 'active' : ''}`} style={{border: newDb.backup_enabled ? '2px solid #10b981' : '1px solid #e5e7eb'}}>
                        <label className="feature-toggle" style={{cursor: 'pointer'}}>
                          <input
                            type="checkbox"
                            checked={newDb.backup_enabled}
                            onChange={(e) => setNewDb({ ...newDb, backup_enabled: e.target.checked })}
                            style={{width: '20px', height: '20px', cursor: 'pointer'}}
                          />
                          <div className="toggle-content">
                            <div className="toggle-header">
                              <span className="toggle-icon" style={{fontSize: '1.5rem'}}>💾</span>
                              <span className="toggle-title" style={{fontSize: '1.1rem', fontWeight: 'bold'}}>Automated Backups</span>
                            </div>
                            <span className="toggle-desc">Enable automated backups with configurable schedule and retention</span>
                          </div>
                        </label>
                        {newDb.backup_enabled && (
                          <div style={{marginTop: '1rem', padding: '1rem', background: '#f0fdf4', borderRadius: '8px', border: '1px solid #86efac'}}>
                            <div style={{marginBottom: '0.75rem'}}>
                              <label style={{display: 'block', marginBottom: '0.5rem', fontWeight: '600', fontSize: '0.9rem'}}>
                                Backup Schedule:
                              </label>
                              <select
                                value={newDb.backup_schedule}
                                onChange={(e) => setNewDb({ ...newDb, backup_schedule: e.target.value })}
                                style={{width: '100%', padding: '0.5rem', borderRadius: '6px', border: '1px solid #d1d5db'}}
                              >
                                <option value="hourly">⏱️ Every Hour</option>
                                <option value="daily">📅 Daily (Recommended)</option>
                                <option value="weekly">📆 Weekly</option>
                              </select>
                            </div>
                            <div>
                              <label style={{display: 'block', marginBottom: '0.5rem', fontWeight: '600', fontSize: '0.9rem'}}>
                                Retention Days: {newDb.backup_retention_days} days
                              </label>
                              <input
                                type="range"
                                min="1"
                                max="365"
                                value={newDb.backup_retention_days}
                                onChange={(e) => setNewDb({ ...newDb, backup_retention_days: parseInt(e.target.value) })}
                                style={{width: '100%'}}
                              />
                              <div style={{display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: '#6b7280', marginTop: '0.25rem'}}>
                                <span>1 day</span>
                                <span>365 days</span>
                              </div>
                            </div>
                            <div style={{marginTop: '0.75rem', padding: '0.5rem', background: '#dbeafe', borderRadius: '6px', fontSize: '0.85rem', color: '#1e40af'}}>
                              ✅ Backups will be stored in S3 and available for {newDb.backup_retention_days} days
                            </div>
                          </div>
                        )}
                      </div>

                      <div className={`feature-card ${newDb.high_availability ? 'active' : ''}`}>
                        <label className="feature-toggle">
                          <input
                            type="checkbox"
                            checked={newDb.high_availability}
                            onChange={(e) => {
                              const haEnabled = e.target.checked
                              let replicas = newDb.replicas
                              
                              // If enabling HA, ensure minimum 3 and odd
                              if (haEnabled) {
                                if (replicas < 3) replicas = 3
                                if (replicas % 2 === 0) replicas = replicas + 1
                              }
                              // If disabling HA, can go to 1 (but keep odd if > 1)
                              else {
                                if (replicas > 1 && replicas % 2 === 0) replicas = replicas - 1
                              }
                              
                              setNewDb({ ...newDb, high_availability: haEnabled, replicas: replicas })
                            }}
                          />
                          <div className="toggle-content">
                            <div className="toggle-header">
                              <span className="toggle-icon">🌐</span>
                              <span className="toggle-title">High Availability</span>
                            </div>
                            <span className="toggle-desc">Multi-AZ deployment for 99.99% uptime</span>
                          </div>
                        </label>
                      </div>

                      <div className={`feature-card ${newDb.monitoring_enabled ? 'active' : ''}`}>
                        <label className="feature-toggle">
                          <input
                            type="checkbox"
                            checked={newDb.monitoring_enabled}
                            onChange={(e) => setNewDb({ ...newDb, monitoring_enabled: e.target.checked })}
                          />
                          <div className="toggle-content">
                            <div className="toggle-header">
                              <span className="toggle-icon">📊</span>
                              <span className="toggle-title">Monitoring</span>
                            </div>
                            <span className="toggle-desc">Real-time metrics and alerting</span>
                          </div>
                        </label>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="modal-footer">
                <button type="button" className="btn-secondary" onClick={() => setShowCreateModal(false)}>
                  Cancel
                </button>
                <button type="submit" className="btn-primary btn-large">
                  🚀 Create Database
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Database Details Modal */}
      {showDetailsModal && selectedDb && (
        <div className="modal-overlay" onClick={() => setShowDetailsModal(false)}>
          <div className="modal modal-large" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h2>
                  <span className="engine-icon">{getEngineIcon(selectedDb.engine)}</span>
                  {selectedDb.name}
                </h2>
                <p className="modal-subtitle">
                  {selectedDb.engine}
                  {selectedDb.engine === 'redis' && selectedDb.version && selectedDb.version.includes('valkey') && (
                    <span style={{color: '#8b5cf6', fontWeight: 600}}> (Valkey)</span>
                  )}
                  {' '}{selectedDb.version}
                </p>
              </div>
              <button className="modal-close" onClick={() => setShowDetailsModal(false)}>×</button>
            </div>

            <div className="tabs">
              <button
                className={activeTab === 'overview' ? 'tab active' : 'tab'}
                onClick={() => setActiveTab('overview')}
              >
                Overview
              </button>
              <button
                className={activeTab === 'status' ? 'tab active' : 'tab'}
                onClick={() => setActiveTab('status')}
              >
                Status
              </button>
              <button
                className={activeTab === 'credentials' ? 'tab active' : 'tab'}
                onClick={() => setActiveTab('credentials')}
              >
                Credentials
              </button>
              <button
                className={activeTab === 'monitoring' ? 'tab active' : 'tab'}
                onClick={() => setActiveTab('monitoring')}
              >
                Monitoring
              </button>
              <button
                className={activeTab === 'actions' ? 'tab active' : 'tab'}
                onClick={() => setActiveTab('actions')}
              >
                Actions
              </button>
              {(selectedDb?.backup_enabled === true || selectedDb?.backup_enabled === 'true') && (
                <button
                  className={activeTab === 'backups' ? 'tab active' : 'tab'}
                  onClick={() => {
                    setActiveTab('backups')
                    if (selectedDb && dbBackups.length === 0) {
                      fetchDbBackups(selectedDb.id)
                    }
                  }}
                  style={{display: 'block'}}
                >
                  💾 Backups
                </button>
              )}
            </div>

            <div className="modal-body">
              {activeTab === 'overview' && (
                <div className="tab-content">
                  <div className="detail-grid">
                    <div className="detail-card">
                      <h4>Configuration</h4>
                      <dl>
                        <dt>Engine</dt>
                        <dd>
                          {selectedDb.engine}
                          {selectedDb.engine === 'redis' && selectedDb.version && selectedDb.version.includes('valkey') && (
                            <span style={{marginLeft: '0.5rem', color: '#8b5cf6', fontSize: '0.85rem'}}>
                              (Valkey)
                            </span>
                          )}
                        </dd>
                        <dt>Version</dt>
                        <dd>
                          {selectedDb.version}
                          <button
                            onClick={() => {
                              fetchAvailableUpgrades(selectedDb.id)
                              setShowUpgradeModal(true)
                            }}
                            style={{
                              marginLeft: '10px',
                              padding: '2px 8px',
                              fontSize: '12px',
                              background: '#3b82f6',
                              color: 'white',
                              border: 'none',
                              borderRadius: '4px',
                              cursor: 'pointer'
                            }}
                          >
                            Upgrade
                          </button>
                        </dd>
                        <dt>Size</dt><dd>{selectedDb.size}</dd>
                        <dt>Storage</dt><dd>{selectedDb.storage_gb} GB</dd>
                      </dl>
                    </div>
                    <div className="detail-card">
                      <h4>Replication</h4>
                      <dl>
                        <dt>Replicas</dt>
                        <dd className="replica-status">
                          <span className={selectedDb.ready_replicas === selectedDb.replicas ? 'healthy' : 'warning'}>
                            {selectedDb.ready_replicas}/{selectedDb.replicas}
                          </span>
                        </dd>
                        <dt>High Availability</dt>
                        <dd>{selectedDb.high_availability ? 'Enabled ✅' : 'Disabled'}</dd>
                      </dl>
                    </div>
                    <div className="detail-card">
                      <h4>Features</h4>
                      <dl>
                        <dt>Backups</dt>
                        <dd>{selectedDb.backup_enabled ? `Enabled (${selectedDb.backup_schedule})` : 'Disabled'}</dd>
                        <dt>Monitoring</dt>
                        <dd>{selectedDb.monitoring_enabled ? 'Enabled ✅' : 'Disabled'}</dd>
                      </dl>
                    </div>
                    <div className="detail-card">
                      <h4>Connection</h4>
                      <dl>
                        <dt>Endpoint</dt>
                        <dd><code>{selectedDb.endpoint || 'Not ready'}</code></dd>
                        <dt>Port</dt>
                        <dd>{selectedDb.port || 'N/A'}</dd>
                      </dl>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'status' && dbStatus && (
                <div className="tab-content">
                  <div className="status-overview">
                    <div className="status-item">
                      <span className="status-label">Status Phase</span>
                      <span className="status-value">{dbStatus.kubedb_phase || 'Unknown'}</span>
                    </div>
                    <div className="status-item">
                      <span className="status-label">Ready</span>
                      <span className="status-value">{dbStatus.is_ready ? '✅ Yes' : '❌ No'}</span>
                    </div>
                    <div className="status-item">
                      <span className="status-label">Replicas</span>
                      <span className="status-value">
                        {dbStatus.replicas?.ready || 0}/{dbStatus.replicas?.desired || 0}
                      </span>
                    </div>
                  </div>
                  {dbStatus.message && (
                    <div className="status-message">
                      <strong>Message:</strong> {dbStatus.message}
                    </div>
                  )}
                  {dbStatus.events && dbStatus.events.length > 0 && (
                    <div className="events-section">
                      <h4>⚠️ Recent Events</h4>
                      <div className="events-list">
                        {dbStatus.events.map((event, i) => (
                          <div key={i} className={`event-item event-${event.type.toLowerCase()}`}>
                            <div className="event-header">
                              <span className="event-reason">{event.reason}</span>
                              <span className="event-count">×{event.count}</span>
                            </div>
                            <p className="event-message">{event.message}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {dbStatus.conditions && dbStatus.conditions.length > 0 && (
                    <div className="conditions">
                      <h4>Conditions</h4>
                      <table>
                        <thead>
                          <tr>
                            <th>Type</th>
                            <th>Status</th>
                            <th>Message</th>
                          </tr>
                        </thead>
                        <tbody>
                          {dbStatus.conditions.map((c, i) => (
                            <tr key={i}>
                              <td>{c.type}</td>
                              <td>{c.status}</td>
                              <td>{c.message}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}

              {activeTab === 'credentials' && (
                <div className="tab-content">
                  {dbCredentials ? (
                    <div className="credentials-box">
                      <div className="cred-item">
                        <label>Username</label>
                        <code>{dbCredentials.username}</code>
                      </div>
                      <div className="cred-item">
                        <label>Password</label>
                        <code>{dbCredentials.password}</code>
                      </div>
                      <div className="cred-item">
                        <label>Host</label>
                        <code>{dbCredentials.host}</code>
                      </div>
                      <div className="cred-item">
                        <label>Port</label>
                        <code>{dbCredentials.port}</code>
                      </div>
                      <div className="cred-item full-width">
                        <label>Connection String</label>
                        <code className="connection-string">{dbCredentials.connection_string}</code>
                      </div>
                    </div>
                  ) : (
                    <div className="empty-message">
                      Credentials not available yet. Wait for database to be ready.
                    </div>
                  )}
                </div>
              )}

              {activeTab === 'monitoring' && (
                <div className="tab-content">
                  <div className="monitoring-section">
                    <div className="monitoring-status-card">
                      <div className="monitoring-status-header">
                        <h4>📊 Monitoring Status</h4>
                        <span className={`status-badge ${selectedDb.monitoring_enabled ? 'status-enabled' : 'status-disabled'}`}>
                          {selectedDb.monitoring_enabled ? 'Enabled ✅' : 'Disabled ❌'}
                        </span>
                      </div>

                      {selectedDb.monitoring_enabled ? (
                        <div className="monitoring-info">
                          <div className="monitoring-header-actions">
                            <p className="monitoring-description">
                              Monitoring is active for this database. Real-time metrics via Kubernetes service endpoints (no port-forward required).
                            </p>
                            <button
                              className="btn-secondary"
                              onClick={() => fetchDbMetrics(selectedDb.id)}
                              disabled={metricsLoading}
                            >
                              {metricsLoading ? 'Loading...' : dbMetrics ? 'Refresh Metrics' : 'Load Metrics'}
                            </button>
                          </div>

                          {dbMetrics && !metricsLoading && (
                            <div className="metrics-display">
                              {dbMetrics.available === false ? (
                                <div className="monitoring-unavailable">
                                  <p>Metrics are currently unavailable. The monitoring service may be starting up.</p>
                                  <p className="monitoring-note">Database: {dbMetrics.database}</p>
                                </div>
                              ) : (
                                <>
                                  <h5>📊 Current Metrics</h5>

                                  {/* Active Connections */}
                                  {dbMetrics.connections && (
                                    <div className="metrics-section">
                                      <h6>🔗 Active Connections</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.connections.active !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">👥</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Active</span>
                                              <span className="metric-value">{dbMetrics.connections.active}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.connections.available !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">✅</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Available</span>
                                              <span className="metric-value">{dbMetrics.connections.available}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.connections.total !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🎯</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Total</span>
                                              <span className="metric-value">{dbMetrics.connections.total}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.connections.used_percent !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📊</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Usage</span>
                                              <span className="metric-value">{dbMetrics.connections.used_percent.toFixed(1)}%</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Read/Write Operations */}
                                  {dbMetrics.operations && (
                                    <div className="metrics-section">
                                      <h6>📊 Database Operations (Read/Write)</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.operations.reads !== undefined && (
                                          <div className="metric-value-card highlight-read">
                                            <span className="metric-icon">📖</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Total Reads</span>
                                              <span className="metric-value">{dbMetrics.operations.reads.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.operations.writes !== undefined && (
                                          <div className="metric-value-card highlight-write">
                                            <span className="metric-icon">✍️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Total Writes</span>
                                              <span className="metric-value">{dbMetrics.operations.writes.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.operations.inserts !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">➕</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Inserts</span>
                                              <span className="metric-value">{dbMetrics.operations.inserts.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.operations.updates !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">✏️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Updates</span>
                                              <span className="metric-value">{dbMetrics.operations.updates.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.operations.deletes !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🗑️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Deletes</span>
                                              <span className="metric-value">{dbMetrics.operations.deletes.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.operations.commands !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⚙️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Commands</span>
                                              <span className="metric-value">{dbMetrics.operations.commands.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Load Metrics */}
                                  {dbMetrics.load && Object.keys(dbMetrics.load).length > 0 && (
                                    <div className="metrics-section">
                                      <h6>⚡ System Load</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.load.memory_resident_mb !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🧠</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Memory (Resident)</span>
                                              <span className="metric-value">{dbMetrics.load.memory_resident_mb.toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.load.memory_virtual_mb !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📦</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Memory (Virtual)</span>
                                              <span className="metric-value">{dbMetrics.load.memory_virtual_mb.toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.load.cpu_total_ms !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⚙️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">CPU Time</span>
                                              <span className="metric-value">{(dbMetrics.load.cpu_total_ms / 1000).toFixed(2)} sec</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.load.network_requests !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📡</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Network Requests</span>
                                              <span className="metric-value">{dbMetrics.load.network_requests.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.load.network_in_bytes !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬇️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Network In</span>
                                              <span className="metric-value">{(dbMetrics.load.network_in_bytes / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.load.network_out_bytes !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬆️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Network Out</span>
                                              <span className="metric-value">{(dbMetrics.load.network_out_bytes / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Storage Section */}
                                  {dbMetrics.storage && Object.keys(dbMetrics.storage).length > 0 && (
                                    <div className="metrics-section">
                                      <h6>💾 Storage</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.storage.total_data_size !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📁</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Data Size</span>
                                              <span className="metric-value">{(dbMetrics.storage.total_data_size / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.storage.total_storage_size !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">💿</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Storage Size</span>
                                              <span className="metric-value">{(dbMetrics.storage.total_storage_size / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.storage.total_collections !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📚</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Collections</span>
                                              <span className="metric-value">{dbMetrics.storage.total_collections}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.storage.total_indexes !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🔍</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Indexes</span>
                                              <span className="metric-value">{dbMetrics.storage.total_indexes}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Network Section (MongoDB) */}
                                  {dbMetrics.network && (
                                    <div className="metrics-section">
                                      <h6>🌐 Network</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.network.bytes_in !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬇️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Bytes In</span>
                                              <span className="metric-value">{(dbMetrics.network.bytes_in / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.network.bytes_out !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬆️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Bytes Out</span>
                                              <span className="metric-value">{(dbMetrics.network.bytes_out / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.network.requests !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📡</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Requests</span>
                                              <span className="metric-value">{dbMetrics.network.requests.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.network.bytes_received !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬇️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Received</span>
                                              <span className="metric-value">{(dbMetrics.network.bytes_received / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.network.bytes_sent !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⬆️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Sent</span>
                                              <span className="metric-value">{(dbMetrics.network.bytes_sent / (1024**2)).toFixed(2)} MB</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Transactions Section (PostgreSQL) */}
                                  {dbMetrics.transactions && (
                                    <div className="metrics-section">
                                      <h6>💼 Transactions</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.transactions.commits !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">✅</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Commits</span>
                                              <span className="metric-value">{dbMetrics.transactions.commits.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.transactions.rollbacks !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">↩️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Rollbacks</span>
                                              <span className="metric-value">{dbMetrics.transactions.rollbacks.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Activity Section (PostgreSQL) */}
                                  {dbMetrics.activity && (
                                    <div className="metrics-section">
                                      <h6>📈 Activity</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.activity.rows_inserted !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">➕</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Rows Inserted</span>
                                              <span className="metric-value">{dbMetrics.activity.rows_inserted.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.activity.rows_updated !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">✏️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Rows Updated</span>
                                              <span className="metric-value">{dbMetrics.activity.rows_updated.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.activity.rows_deleted !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🗑️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Rows Deleted</span>
                                              <span className="metric-value">{dbMetrics.activity.rows_deleted.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.activity.rows_fetched !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📥</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Rows Fetched</span>
                                              <span className="metric-value">{dbMetrics.activity.rows_fetched.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Cache Section (PostgreSQL) */}
                                  {dbMetrics.cache && (
                                    <div className="metrics-section">
                                      <h6>⚡ Cache</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.cache.hits !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🎯</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Hits</span>
                                              <span className="metric-value">{dbMetrics.cache.hits.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.cache.reads !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📖</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Reads</span>
                                              <span className="metric-value">{dbMetrics.cache.reads.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.cache.hit_ratio !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📊</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Hit Ratio</span>
                                              <span className="metric-value">{dbMetrics.cache.hit_ratio.toFixed(1)}%</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Queries Section (MySQL) */}
                                  {dbMetrics.queries && (
                                    <div className="metrics-section">
                                      <h6>🔍 Queries</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.queries.total !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📊</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Total</span>
                                              <span className="metric-value">{dbMetrics.queries.total.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.queries.slow !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🐌</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Slow Queries</span>
                                              <span className="metric-value">{dbMetrics.queries.slow.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* System Section */}
                                  {dbMetrics.system && (
                                    <div className="metrics-section">
                                      <h6>🖥️ System</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.system.cpu_count !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⚡</span>
                                            <div className="metric-content">
                                              <span className="metric-label">CPU Count</span>
                                              <span className="metric-value">{dbMetrics.system.cpu_count}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.system.memory_total_kb !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🧠</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Total Memory</span>
                                              <span className="metric-value">{(dbMetrics.system.memory_total_kb / (1024**2)).toFixed(2)} GB</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.system.memory_available_kb !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">✅</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Available Memory</span>
                                              <span className="metric-value">{(dbMetrics.system.memory_available_kb / (1024**2)).toFixed(2)} GB</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Replication & Healthy Replicas */}
                                  {dbMetrics.replication && Object.keys(dbMetrics.replication).length > 0 && (
                                    <div className="metrics-section">
                                      <h6>🔄 Replication & Healthy Replicas</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.replication.healthy_replicas !== undefined && (
                                          <div className="metric-value-card highlight-health">
                                            <span className="metric-icon">✅</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Healthy Replicas</span>
                                              <span className="metric-value">{dbMetrics.replication.healthy_replicas}/{dbMetrics.replication.total_replicas || 0}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.replication.health_percent !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📊</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Health %</span>
                                              <span className="metric-value">{dbMetrics.replication.health_percent.toFixed(1)}%</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.replication.state !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🔗</span>
                                            <div className="metric-content">
                                              <span className="metric-label">State</span>
                                              <span className="metric-value">{dbMetrics.replication.state}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.replication.lag_seconds !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">⏱️</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Replication Lag</span>
                                              <span className="metric-value">{dbMetrics.replication.lag_seconds.toFixed(2)}s</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* InnoDB Section (MySQL) */}
                                  {dbMetrics.innodb && (
                                    <div className="metrics-section">
                                      <h6>⚙️ InnoDB</h6>
                                      <div className="metrics-grid-values">
                                        {dbMetrics.innodb.buffer_pool_pages !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">📄</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Buffer Pool Pages</span>
                                              <span className="metric-value">{dbMetrics.innodb.buffer_pool_pages.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                        {dbMetrics.innodb.buffer_pool_free_pages !== undefined && (
                                          <div className="metric-value-card">
                                            <span className="metric-icon">🆓</span>
                                            <div className="metric-content">
                                              <span className="metric-label">Free Pages</span>
                                              <span className="metric-value">{dbMetrics.innodb.buffer_pool_free_pages.toLocaleString()}</span>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  <div className="metrics-footer">
                                    <small>Last updated: {dbMetrics.timestamp ? new Date(dbMetrics.timestamp).toLocaleString() : 'N/A'}</small>
                                  </div>
                                </>
                              )}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="monitoring-disabled">
                          <p>Monitoring is not enabled for this database.</p>
                          <p className="monitoring-note">
                            To enable monitoring, you need to create the database with <code>monitoring_enabled: true</code> in the configuration.
                          </p>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'actions' && (
                <div className="tab-content">
                  <div className="actions-grid">
                    <div className="action-card">
                      <h4>💻 Change Instance Size</h4>
                      <p>Vertical scaling - Update CPU and memory allocation</p>
                      <select
                        className="select-large"
                        defaultValue={selectedDb.size}
                        onChange={(e) => {
                          if (confirm(`Change instance size to ${e.target.value}? This will restart the database.`)) {
                            updateDatabaseConfig(selectedDb.id, { size: e.target.value })
                          } else {
                            e.target.value = selectedDb.size
                          }
                        }}
                      >
                        <option value="db.t3.micro">💻 Micro - 0.5 CPU, 1GB RAM</option>
                        <option value="db.t3.small">📊 Small - 1 CPU, 2GB RAM</option>
                        <option value="db.t3.medium">🚀 Medium - 2 CPU, 4GB RAM</option>
                        <option value="db.t3.large">⚡ Large - 2 CPU, 8GB RAM</option>
                        <option value="db.t3.xlarge">🔥 XLarge - 4 CPU, 16GB RAM</option>
                        <option value="db.t3.2xlarge">💪 2XLarge - 8 CPU, 32GB RAM</option>
                      </select>
                      <small style={{color: '#666', marginTop: '0.5rem'}}>Current: {selectedDb.size}</small>
                    </div>

                    <div className="action-card">
                      <h4>💾 Expand Storage</h4>
                      <p>Increase storage size (cannot be decreased)</p>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          const newSize = prompt(
                            `Current storage: ${selectedDb.storage_gb} GB\nEnter new size (GB). Must be larger than current:`,
                            selectedDb.storage_gb + 10
                          )
                          if (newSize) {
                            const size = parseInt(newSize)
                            if (size <= selectedDb.storage_gb) {
                              showNotification('Storage size must be larger than current size', 'error')
                            } else {
                              updateDatabaseConfig(selectedDb.id, { storage_gb: size })
                            }
                          }
                        }}
                      >
                        Expand Storage
                      </button>
                      <small style={{color: '#666', marginTop: '0.5rem'}}>Current: {selectedDb.storage_gb} GB</small>
                    </div>

                    <div className="action-card">
                      <h4>⚖️ Scale Replicas</h4>
                      <p>Horizontal scaling - Add or remove replicas</p>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          const n = prompt(`Current: ${selectedDb.replicas}. Enter new count:`, selectedDb.replicas)
                          if (n) {
                            const count = parseInt(n)
                            if (count > 0 && count <= 10) {
                              updateDatabaseConfig(selectedDb.id, { replicas: count })
                            } else {
                              showNotification('Replica count must be between 1 and 10', 'error')
                            }
                          }
                        }}
                      >
                        Scale Replicas
                      </button>
                      <small style={{color: '#666', marginTop: '0.5rem'}}>Current: {selectedDb.replicas} replica(s)</small>
                    </div>

                    <div className="action-card">
                      <h4>⏸️ Pause Database</h4>
                      <p>Temporarily pause the database to save costs</p>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          pauseDatabase(selectedDb.id)
                          setShowDetailsModal(false)
                        }}
                        disabled={selectedDb.status === 'paused'}
                      >
                        Pause
                      </button>
                    </div>

                    <div className="action-card">
                      <h4>▶️ Resume Database</h4>
                      <p>Resume a paused database</p>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          resumeDatabase(selectedDb.id)
                          setShowDetailsModal(false)
                        }}
                        disabled={selectedDb.status !== 'paused'}
                      >
                        Resume
                      </button>
                    </div>

                    <div className="action-card danger">
                      <h4>🗑️ Delete Database</h4>
                      <p>Permanently delete this database and all data</p>
                      <button
                        className="btn-danger"
                        onClick={() => deleteDatabase(selectedDb.id, selectedDb.name)}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'backups' && (
                <div className="tab-content">
                  <div className="backups-section">
                    <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem'}}>
                      <h4>💾 Database Backups</h4>
                      <button
                        className="btn-primary"
                        onClick={() => triggerBackup(selectedDb.id)}
                        disabled={selectedDb.status !== 'running'}
                        style={{padding: '0.5rem 1rem'}}
                      >
                        + Trigger Backup
                      </button>
                    </div>

                    {backupsLoading ? (
                      <div style={{textAlign: 'center', padding: '2rem'}}>Loading backups...</div>
                    ) : dbBackups.length === 0 ? (
                      <div style={{textAlign: 'center', padding: '2rem', color: '#6b7280'}}>
                        <p>No backups found</p>
                        <p style={{fontSize: '0.9rem', marginTop: '0.5rem'}}>Trigger a backup to get started</p>
                      </div>
                    ) : (
                      <div className="backups-list">
                        {dbBackups.map((backup, idx) => {
                          const getStatusColor = (phase) => {
                            if (phase === 'Succeeded' || phase === 'Complete') return '#10b981'
                            if (phase === 'Failed') return '#ef4444'
                            if (phase === 'Running') return '#3b82f6'
                            return '#6b7280'
                          }
                          
                          const getStatusIcon = (phase) => {
                            if (phase === 'Succeeded' || phase === 'Complete') return '✅'
                            if (phase === 'Failed') return '❌'
                            if (phase === 'Running') return '🔄'
                            return '⏳'
                          }

                          // Extract timestamp from backup_id if available
                          let backupDate = backup.created_at || backup.backup_id
                          if (backup.backup_id && backup.backup_id.includes('-')) {
                            const parts = backup.backup_id.split('-')
                            const timestamp = parts[parts.length - 1]
                            if (timestamp && timestamp.length === 15) {
                              // Format: 20251222-174930
                              const dateStr = timestamp.substring(0, 8)
                              const timeStr = timestamp.substring(9, 15)
                              backupDate = `${dateStr} ${timeStr.substring(0, 2)}:${timeStr.substring(2, 4)}:${timeStr.substring(4, 6)}`
                            }
                          }

                          return (
                            <div key={idx} className="backup-item" style={{
                              border: '1px solid #e5e7eb',
                              borderRadius: '8px',
                              padding: '1rem',
                              marginBottom: '1rem',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center'
                            }}>
                              <div style={{flex: 1}}>
                                <div style={{display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem'}}>
                                  <span style={{fontSize: '1.2rem'}}>{getStatusIcon(backup.phase)}</span>
                                  <strong>{backup.backup_id}</strong>
                                  <span style={{
                                    backgroundColor: getStatusColor(backup.phase),
                                    color: 'white',
                                    padding: '0.25rem 0.5rem',
                                    borderRadius: '4px',
                                    fontSize: '0.75rem',
                                    fontWeight: 'bold'
                                  }}>
                                    {backup.phase}
                                  </span>
                                </div>
                                <div style={{fontSize: '0.85rem', color: '#6b7280'}}>
                                  <div>Created: {backupDate}</div>
                                  {backup.type && <div>Type: {backup.type}</div>}
                                </div>
                              </div>
                              <div>
                                {(backup.phase === 'Succeeded' || backup.phase === 'Complete') && (
                                  <button
                                    className="btn-secondary"
                                    onClick={() => {
                                      // Extract timestamp from backup_id for restore
                                      let restoreId = backup.backup_id
                                      if (backup.backup_id.includes('backup-')) {
                                        restoreId = backup.backup_id.split('backup-')[1]
                                      }
                                      restoreDatabase(selectedDb.id, restoreId)
                                    }}
                                    style={{padding: '0.5rem 1rem', marginLeft: '0.5rem'}}
                                  >
                                    🔄 Restore
                                  </button>
                                )}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Upgrade Modal */}
      {showUpgradeModal && selectedDb && (
        <div className="modal-overlay" onClick={() => setShowUpgradeModal(false)}>
          <div className="modal-content" style={{maxWidth: '600px'}} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Upgrade {selectedDb.name}</h2>
              <button className="close-btn" onClick={() => setShowUpgradeModal(false)}>×</button>
            </div>
            <div className="modal-body">
              <div style={{
                display: 'flex',
                gap: '20px',
                padding: '16px',
                background: '#f9fafb',
                borderRadius: '6px',
                marginBottom: '20px'
              }}>
                <div>
                  <div style={{fontSize: '12px', color: '#6b7280', marginBottom: '4px'}}>Current Version</div>
                  <div style={{fontSize: '20px', fontWeight: '600', color: '#1f2937'}}>{selectedDb.version}</div>
                </div>
                <div style={{fontSize: '24px', color: '#9ca3af', alignSelf: 'center'}}>→</div>
                <div>
                  <div style={{fontSize: '12px', color: '#6b7280', marginBottom: '4px'}}>Engine</div>
                  <div style={{fontSize: '16px', fontWeight: '500', color: '#4b5563', textTransform: 'uppercase'}}>{selectedDb.engine}</div>
                </div>
              </div>

              {upgradeLoading ? (
                <div style={{
                  padding: '40px 20px',
                  textAlign: 'center',
                  background: '#f9fafb',
                  borderRadius: '6px'
                }}>
                  <div style={{
                    width: '40px',
                    height: '40px',
                    border: '4px solid #e5e7eb',
                    borderTop: '4px solid #3b82f6',
                    borderRadius: '50%',
                    margin: '0 auto 16px',
                    animation: 'spin 1s linear infinite'
                  }}></div>
                  <div style={{fontSize: '14px', color: '#6b7280'}}>Checking for available upgrades...</div>
                </div>
              ) : availableUpgrades.length > 0 ? (
                <>
                  <div style={{marginBottom: '20px'}}>
                    <label htmlFor="upgrade-version" style={{
                      display: 'block',
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#374151',
                      marginBottom: '12px'
                    }}>
                      Select Target Version
                    </label>
                    <div style={{display: 'flex', flexDirection: 'column', gap: '8px'}}>
                      {availableUpgrades.map(version => {
                        const upgradeType = getUpgradeType(selectedDb.version, version)
                        const isSelected = selectedUpgradeVersion === version
                        const badgeColors = {
                          'PATCH': { bg: '#dbeafe', text: '#1e40af', border: '#93c5fd' },
                          'MINOR': { bg: '#fef3c7', text: '#92400e', border: '#fcd34d' },
                          'MAJOR': { bg: '#fee2e2', text: '#991b1b', border: '#fca5a5' }
                        }
                        const colors = badgeColors[upgradeType]
                        const upgradeInfo = {
                          'PATCH': { icon: '🔧', desc: 'Bug fixes and security patches', time: '~5-10 min' },
                          'MINOR': { icon: '⚡', desc: 'New features, backward compatible', time: '~10-15 min' },
                          'MAJOR': { icon: '🚀', desc: 'Breaking changes, review required', time: '~15-30 min' }
                        }
                        const info = upgradeInfo[upgradeType]

                        return (
                          <div
                            key={version}
                            onClick={() => setSelectedUpgradeVersion(version)}
                            style={{
                              padding: '14px',
                              border: `2px solid ${isSelected ? '#3b82f6' : '#e5e7eb'}`,
                              borderRadius: '6px',
                              cursor: 'pointer',
                              background: isSelected ? '#eff6ff' : 'white',
                              transition: 'all 0.2s'
                            }}
                          >
                            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'start'}}>
                              <div style={{flex: 1}}>
                                <div style={{display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px'}}>
                                  <span style={{fontSize: '16px', fontWeight: '600', color: '#111827'}}>{version}</span>
                                  <span style={{
                                    padding: '3px 10px',
                                    background: colors.bg,
                                    color: colors.text,
                                    border: `1px solid ${colors.border}`,
                                    borderRadius: '12px',
                                    fontSize: '11px',
                                    fontWeight: '600',
                                    letterSpacing: '0.5px'
                                  }}>
                                    {upgradeType}
                                  </span>
                                </div>
                                <div style={{fontSize: '13px', color: '#6b7280', marginBottom: '4px'}}>
                                  {info.icon} {info.desc}
                                </div>
                                <div style={{fontSize: '12px', color: '#9ca3af'}}>
                                  Estimated time: {info.time}
                                </div>
                              </div>
                              {isSelected && (
                                <div style={{
                                  width: '20px',
                                  height: '20px',
                                  background: '#3b82f6',
                                  borderRadius: '50%',
                                  display: 'flex',
                                  alignItems: 'center',
                                  justifyContent: 'center',
                                  color: 'white',
                                  fontSize: '12px',
                                  fontWeight: '700'
                                }}>✓</div>
                              )}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  {selectedUpgradeVersion && (
                    <div style={{
                      marginBottom: '20px',
                      padding: '14px',
                      background: '#fef3c7',
                      border: '1px solid #f59e0b',
                      borderRadius: '6px',
                      fontSize: '13px'
                    }}>
                      <div style={{fontWeight: '600', marginBottom: '8px', color: '#92400e'}}>
                        ⚠️ Before You Upgrade
                      </div>
                      <ul style={{margin: '0', paddingLeft: '20px', color: '#78350f', lineHeight: '1.6'}}>
                        <li>A backup will be created automatically</li>
                        <li>Temporary downtime expected during upgrade</li>
                        <li>This operation cannot be undone</li>
                        <li>Test in non-production first if possible</li>
                      </ul>
                    </div>
                  )}

                  <div style={{display: 'flex', gap: '10px', justifyContent: 'flex-end'}}>
                    <button
                      onClick={() => setShowUpgradeModal(false)}
                      style={{
                        padding: '10px 20px',
                        background: '#ffffff',
                        color: '#374151',
                        border: '1px solid #d1d5db',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        fontWeight: '500',
                        fontSize: '14px'
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => upgradeDatabase(selectedDb.id, selectedUpgradeVersion)}
                      disabled={!selectedUpgradeVersion}
                      style={{
                        padding: '10px 24px',
                        background: selectedUpgradeVersion ? '#3b82f6' : '#9ca3af',
                        color: 'white',
                        border: 'none',
                        borderRadius: '6px',
                        cursor: selectedUpgradeVersion ? 'pointer' : 'not-allowed',
                        fontWeight: '600',
                        fontSize: '14px',
                        boxShadow: selectedUpgradeVersion ? '0 1px 2px rgba(0,0,0,0.1)' : 'none'
                      }}
                    >
                      {selectedUpgradeVersion ? `Upgrade to ${selectedUpgradeVersion}` : 'Select a Version'}
                    </button>
                  </div>
                </>
              ) : (
                <div style={{
                  padding: '40px 20px',
                  background: '#f9fafb',
                  borderRadius: '6px',
                  textAlign: 'center'
                }}>
                  <div style={{fontSize: '48px', marginBottom: '12px'}}>✓</div>
                  <div style={{fontSize: '16px', fontWeight: '600', color: '#1f2937', marginBottom: '8px'}}>
                    You're Up to Date!
                  </div>
                  <div style={{fontSize: '14px', color: '#6b7280'}}>
                    No newer versions available for {selectedDb.engine} {selectedDb.version}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Notification Toast */}
      {notification && (
        <div className={`notification notification-${notification.type}`}>
          {notification.message}
        </div>
      )}
    </div>
  )
}

export default App
