import { useState } from 'react'
import App from './App.jsx'
import Providers from './Providers.jsx'
import './Root.css'

function Root() {
  const [currentView, setCurrentView] = useState('databases')
  const [notification, setNotification] = useState(null)

  const showNotification = (message, type = 'success') => {
    setNotification({ message, type })
    setTimeout(() => setNotification(null), 4000)
  }

  return (
    <div className="root-container">
      {/* Navigation Header */}
      <div className="app-header">
        <div className="app-logo">
          <h1>🗄️ KubeDB Platform</h1>
          <p className="app-subtitle">Multi-Provider Database as a Service</p>
        </div>
        <nav className="app-nav">
          <button
            className={`nav-button ${currentView === 'databases' ? 'active' : ''}`}
            onClick={() => setCurrentView('databases')}
          >
            <span className="nav-icon">💾</span>
            <span>Databases</span>
          </button>
          <button
            className={`nav-button ${currentView === 'providers' ? 'active' : ''}`}
            onClick={() => setCurrentView('providers')}
          >
            <span className="nav-icon">☁️</span>
            <span>Providers</span>
          </button>
        </nav>
      </div>

      {/* Notification */}
      {notification && (
        <div className={`notification ${notification.type}`}>
          <span className="notification-icon">
            {notification.type === 'success' ? '✓' : '✕'}
          </span>
          <span>{notification.message}</span>
          <button
            className="notification-close"
            onClick={() => setNotification(null)}
          >
            ×
          </button>
        </div>
      )}

      {/* Main Content */}
      <div className="app-content">
        {currentView === 'databases' ? (
          <App />
        ) : (
          <Providers showNotification={showNotification} />
        )}
      </div>
    </div>
  )
}

export default Root
