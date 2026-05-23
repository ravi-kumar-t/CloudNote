import React, { useState, useEffect } from 'react';
import { LogOut, BookOpen, Search, FileText, Calendar, Compass, ListChecks, HelpCircle, X } from 'lucide-react';
import './App.css';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

function App() {
  const [token, setToken] = useState(localStorage.getItem('token') || '');
  const [currentUser, setCurrentUser] = useState(localStorage.getItem('username') || '');
  const [view, setView] = useState('auth'); // 'auth' or 'dashboard'
  const [authTab, setAuthTab] = useState('login'); // 'login' or 'register'
  
  // Auth Form State
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [authError, setAuthError] = useState('');
  const [authSuccess, setAuthSuccess] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  // Dashboard State
  const [summaries, setSummaries] = useState([]);
  const [selectedSummary, setSelectedSummary] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [dashboardError, setDashboardError] = useState('');
  const [ingestionStatus, setIngestionStatus] = useState(null);
  const [timetable, setTimetable] = useState([]);
  const [sessionStatus, setSessionStatus] = useState(null);
  const [showScreenshot, setShowScreenshot] = useState(false);
  const [modalScreenshot, setModalScreenshot] = useState(null);

  // Timetable Fetching
  const fetchTimetable = async (authToken) => {
    try {
      const res = await fetch(`${API_BASE}/api/timetable`, {
        headers: {
          'Authorization': `Bearer ${authToken}`
        }
      });
      if (res.ok) {
        const data = await res.json();
        setTimetable(data);
      }
    } catch (err) {
      console.error('Failed to fetch timetable:', err);
    }
  };

  // Synchronize token state
  useEffect(() => {
    if (token) {
      setView('dashboard');
      fetchSummaries(token);
      fetchTimetable(token);
      fetchSessionStatus(token);
    } else {
      setView('auth');
    }
  }, [token]);

  // Auth Operations
  const handleAuth = async (e) => {
    e.preventDefault();
    setAuthError('');
    setAuthSuccess('');
    setIsLoading(true);

    try {
      if (authTab === 'login') {
        const res = await fetch(`${API_BASE}/api/auth/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        
        if (!res.ok) {
          throw new Error(data.detail || 'Failed to authenticate.');
        }

        localStorage.setItem('token', data.access_token);
        localStorage.setItem('username', data.username);
        setToken(data.access_token);
        setCurrentUser(data.username);
        
        // Reset inputs
        setUsername('');
        setPassword('');
      } else {
        const res = await fetch(`${API_BASE}/api/auth/register`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, email })
        });
        const data = await res.json();

        if (!res.ok) {
          throw new Error(data.detail || 'Failed to register student account.');
        }

        setAuthSuccess('Student account created successfully! Please log in.');
        setAuthTab('login');
        setPassword('');
        setEmail('');
      }
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('username');
    setToken('');
    setCurrentUser('');
    setSummaries([]);
    setView('auth');
  };

  // Summary Fetching
  const fetchSummaries = async (authToken) => {
    setDashboardError('');
    try {
      const res = await fetch(`${API_BASE}/api/summaries`, {
        headers: {
          'Authorization': `Bearer ${authToken}`
        }
      });
      const data = await res.json();

      if (!res.ok) {
        if (res.status === 401) {
          handleLogout();
          return;
        }
        throw new Error(data.detail || 'Failed to load summaries.');
      }

      setSummaries(data);
    } catch (err) {
      setDashboardError(err.message);
    }
  };

  // Live Ingestion Status Fetching
  const fetchIngestionStatus = async (authToken) => {
    try {
      const res = await fetch(`${API_BASE}/api/ingestion/status`, {
        headers: {
          'Authorization': `Bearer ${authToken}`
        }
      });
      if (res.ok) {
        const data = await res.json();
        setIngestionStatus(data);
      }
    } catch (err) {
      console.error('Failed to fetch ingestion status:', err);
    }
  };

  // Live Session Status Fetching (screenshots)
  const fetchSessionStatus = async (authToken) => {
    try {
      const res = await fetch(`${API_BASE}/api/session-status`, {
        headers: {
          'Authorization': `Bearer ${authToken}`
        }
      });
      if (res.ok) {
        const data = await res.json();
        setSessionStatus(data);
      }
    } catch (err) {
      console.error('Failed to fetch session status:', err);
    }
  };

  // Poll Ingestion Status & Timetable
  useEffect(() => {
    if (token) {
      fetchIngestionStatus(token);
      fetchSessionStatus(token);
      fetchTimetable(token);
      const interval = setInterval(() => {
        fetchIngestionStatus(token);
        fetchSessionStatus(token);
        fetchTimetable(token);
      }, 10000);
      return () => clearInterval(interval);
    }
  }, [token]);

  // Search Filter
  const filteredSummaries = summaries.filter(s => {
    const query = searchQuery.toLowerCase();
    return (
      s.subject.toLowerCase().includes(query) ||
      s.summary.toLowerCase().includes(query) ||
      s.topics.some(t => t.toLowerCase().includes(query))
    );
  });

  return (
    <div className="dashboard-container">
      {/* Navbar visible on dashboard */}
      {view === 'dashboard' && (
        <header className="navbar">
          <div className="brand">
            <BookOpen size={28} color="#a855f7" />
            <span>CloudNote</span>
            <span className="brand-subtitle">Student Portal</span>
          </div>
          <div className="user-profile-badge">
            <span className="username-display">Welcome, <strong>{currentUser}</strong></span>
            <button className="btn-logout" onClick={handleLogout}>
              <LogOut size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
              Logout
            </button>
          </div>
        </header>
      )}

      {/* Main View Router */}
      {view === 'auth' ? (
        <div className="auth-wrapper">
          <div className="auth-card glass-panel">
            <h2 className="auth-logo">CloudNote</h2>
            <p className="auth-subtitle">AI-Powered Lecture Intelligence Platform</p>
            
            <div className="auth-tabs">
              <button 
                className={`auth-tab ${authTab === 'login' ? 'active' : ''}`}
                onClick={() => { setAuthTab('login'); setAuthError(''); setAuthSuccess(''); }}
              >
                Log In
              </button>
              <button 
                className={`auth-tab ${authTab === 'register' ? 'active' : ''}`}
                onClick={() => { setAuthTab('register'); setAuthError(''); setAuthSuccess(''); }}
              >
                Register
              </button>
            </div>

            {authError && <div className="auth-error">{authError}</div>}
            {authSuccess && <div className="auth-success">{authSuccess}</div>}

            <form onSubmit={handleAuth}>
              <div className="form-group">
                <label>LPU Username</label>
                <input 
                  type="text" 
                  className="input-field" 
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="e.g. 12014633"
                  required
                />
              </div>

              {authTab === 'register' && (
                <div className="form-group">
                  <label>University Email</label>
                  <input 
                    type="email" 
                    className="input-field" 
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="e.g. name@lpu.in"
                    required
                  />
                </div>
              )}

              <div className="form-group">
                <label>Secure Password</label>
                <input 
                  type="password" 
                  className="input-field" 
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                />
              </div>

              <button 
                type="submit" 
                className="btn-primary" 
                disabled={isLoading}
              >
                {isLoading ? 'Processing...' : authTab === 'login' ? 'Enter Dashboard' : 'Create Profile'}
              </button>
            </form>
          </div>
        </div>
      ) : (
        <main className="dashboard-content">
          <div className="dashboard-header">
            <div className="dashboard-welcome">
              <h1>Lecture Summaries</h1>
              <p>Review active summaries auto-ingested from your live CodeTantra classes.</p>
            </div>
          </div>

          {/* Active Ingestion Status Banner */}
          {ingestionStatus && (
            <div className={`ingestion-status-banner glass-panel ${ingestionStatus.status}`}>
              <div className="status-info">
                <span className={`status-pulse-dot ${ingestionStatus.status}`}></span>
                <strong style={{ textTransform: 'capitalize' }}>
                  Bot State: {ingestionStatus.status === 'processing' ? 'Active Ingestion' : ingestionStatus.status}
                </strong>
                {ingestionStatus.details && <span className="status-details"> — {ingestionStatus.details}</span>}
                {ingestionStatus.subject && <span className="status-subject"> [{ingestionStatus.subject}]</span>}
                {ingestionStatus.error && <span className="status-error-txt"> ({ingestionStatus.error})</span>}
              </div>
              <div className="status-time">
                <span className="status-timestamp">Last Active: {ingestionStatus.timestamp}</span>
              </div>
            </div>
          )}

          {/* Timetable Monitor Widget */}
          <div className="timetable-section glass-panel">
            <div className="timetable-header">
              <div className="timetable-title">
                <Calendar size={22} color="#a855f7" />
                <h2>Today's Class Schedule</h2>
              </div>
            </div>
            
            {timetable.length === 0 ? (
              <div className="timetable-empty">
                <p>No classes scheduled for today.</p>
                <p className="timetable-empty-sub">Schedules are automatically fetched and updated by the background intelligence loop.</p>
              </div>
            ) : (
              <div className="timetable-grid">
                {timetable.map((c) => (
                  <div key={c.key} className={`timetable-card ${c.status.toLowerCase()}`}>
                    <div className="card-badge-row">
                      <span className="subject-code-badge">{c.subject_code}</span>
                      <span className={`status-badge ${c.status.toLowerCase()}`}>
                        {c.status.replace('_', ' ')}
                      </span>
                    </div>
                    <h3 className="subject-name">{c.subject_name}</h3>
                    <p className="faculty-name">Instructor: {c.faculty}</p>
                    <div className="class-timing-row">
                      <span className="time-display">{c.timings}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Live Validation Screenshot Monitoring */}
          {sessionStatus && (
            <div className="session-status-card glass-panel">
              <div className="session-status-info">
                {(() => {
                  switch (sessionStatus.status) {
                    case 'CONNECTED':
                      return (
                        <>
                          <span className="status-indicator connected">
                            <span className="pulse-circle connected"></span>
                            🟢 Connected to Class
                          </span>
                          <span className="status-time-lbl">
                            Last joined: <strong>{sessionStatus.last_join_time ? new Date(sessionStatus.last_join_time.replace(/-/g, '/')).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'N/A'}</strong>
                          </span>
                        </>
                      );
                    case 'CONNECTING':
                      return (
                        <>
                          <span className="status-indicator connecting">
                            <span className="pulse-circle connecting"></span>
                            🟡 Connecting to Class...
                          </span>
                          <span className="status-time-lbl">
                            Initializing classroom session
                          </span>
                        </>
                      );
                    case 'RECOVERING':
                      return (
                        <>
                          <span className="status-indicator recovering">
                            <span className="pulse-circle recovering"></span>
                            🟠 Recovering Connection...
                          </span>
                          <span className="status-time-lbl">
                            Executing automated reconnection watchdog
                          </span>
                        </>
                      );
                    case 'DISCONNECTED':
                      return (
                        <>
                          <span className="status-indicator disconnected">
                            <span className="pulse-circle disconnected"></span>
                            🔴 Disconnected
                          </span>
                          <span className="status-time-lbl">
                            {sessionStatus.disconnect_time ? (
                              <>Disconnected at: <strong>{new Date(sessionStatus.disconnect_time.replace(/-/g, '/')).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</strong></>
                            ) : (
                              'No active session currently connected.'
                            )}
                          </span>
                        </>
                      );
                    case 'IDLE':
                    default:
                      return (
                        <>
                          <span className="status-indicator idle">
                            <span className="pulse-circle idle"></span>
                            🟣 Standby / Idle
                          </span>
                          <span className="status-time-lbl">
                            Background worker monitoring schedule
                          </span>
                        </>
                      );
                  }
                })()}
              </div>
              
              {sessionStatus.screenshot && (
                <button 
                  className="btn-view-screenshot" 
                  onClick={() => setModalScreenshot({
                    title: sessionStatus.status === 'CONNECTED' ? '🟢 Join Success Validation Screenshot' : '🔴 Disconnect Validation Screenshot',
                    meta: `Timestamp: ${sessionStatus.status === 'CONNECTED' ? sessionStatus.last_join_time : sessionStatus.disconnect_time}`,
                    src: `${API_BASE}/screenshots/${sessionStatus.screenshot}`
                  })}
                >
                  [View Screenshot]
                </button>
              )}
            </div>
          )}

          {/* Last Session Historical Card */}
          {sessionStatus && sessionStatus.last_session && sessionStatus.last_session.joined_at && (
            <div className="last-session-card glass-panel">
              <div className="last-session-header">
                <Compass size={20} color="#a855f7" />
                <h2>Last Ingested Session Attendance Proof</h2>
              </div>
              <div className="last-session-body">
                <div className="last-session-meta-grid">
                  <div className="meta-item">
                    <span className="meta-label">Subject / Class</span>
                    <strong className="meta-val">{sessionStatus.last_session.last_completed_class || 'N/A'}</strong>
                  </div>
                  <div className="meta-item">
                    <span className="meta-label">Session Status</span>
                    <span className="status-badge completed">
                      {sessionStatus.last_session.final_session_state || 'COMPLETED'}
                    </span>
                  </div>
                  <div className="meta-item">
                    <span className="meta-label">Joined At</span>
                    <strong className="meta-val">{sessionStatus.last_session.joined_at}</strong>
                  </div>
                  <div className="meta-item">
                    <span className="meta-label">Disconnected At</span>
                    <strong className="meta-val">{sessionStatus.last_session.disconnected_at || 'Active'}</strong>
                  </div>
                  <div className="meta-item">
                    <span className="meta-label">Total Duration</span>
                    <strong className="meta-val">{sessionStatus.last_session.session_duration || 'N/A'}</strong>
                  </div>
                </div>
                
                <div className="last-session-screenshots">
                  {sessionStatus.last_session.latest_join_screenshot && (
                    <div className="screenshot-preview-box">
                      <span className="screenshot-title">🟢 Connection Check</span>
                      <div className="screenshot-wrapper">
                        <img 
                          src={`${API_BASE}/screenshots/${sessionStatus.last_session.latest_join_screenshot}`} 
                          alt="Join Screenshot" 
                          onClick={() => {
                            setModalScreenshot({
                              title: '🟢 Join Success Validation Screenshot',
                              meta: `Timestamp: ${sessionStatus.last_session.joined_at}`,
                              src: `${API_BASE}/screenshots/${sessionStatus.last_session.latest_join_screenshot}`
                            });
                          }}
                        />
                      </div>
                    </div>
                  )}
                  {sessionStatus.last_session.latest_disconnect_screenshot && (
                    <div className="screenshot-preview-box">
                      <span className="screenshot-title">🔴 Disconnect Check</span>
                      <div className="screenshot-wrapper">
                        <img 
                          src={`${API_BASE}/screenshots/${sessionStatus.last_session.latest_disconnect_screenshot}`} 
                          alt="Disconnect Screenshot" 
                          onClick={() => {
                            setModalScreenshot({
                              title: '🔴 Disconnect Validation Screenshot',
                              meta: `Timestamp: ${sessionStatus.last_session.disconnected_at}`,
                              src: `${API_BASE}/screenshots/${sessionStatus.last_session.latest_disconnect_screenshot}`
                            });
                          }}
                        />
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Screenshot Modal Viewer */}
          {modalScreenshot && (
            <div className="modal-overlay" onClick={() => setModalScreenshot(null)}>
              <div className="screenshot-modal-content glass-panel" onClick={(e) => e.stopPropagation()}>
                <button className="btn-close" onClick={() => setModalScreenshot(null)}>
                  <X size={20} />
                </button>
                <h3 className="screenshot-modal-title">{modalScreenshot.title}</h3>
                <p className="screenshot-modal-meta">{modalScreenshot.meta}</p>
                <div className="screenshot-image-container">
                  <img 
                    src={modalScreenshot.src} 
                    alt="Validation Screenshot" 
                    className="validation-screenshot-img"
                  />
                </div>
              </div>
            </div>
          )}

          {/* Controls Bar */}
          <div className="filter-bar">
            <div className="search-wrapper">
              <Search className="search-icon-svg" size={20} />
              <input 
                type="text" 
                className="input-field search-input" 
                placeholder="Search topics, content, keywords..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>

          {dashboardError && <div className="auth-error">{dashboardError}</div>}

          {/* Summaries Feed Grid */}
          {filteredSummaries.length === 0 ? (
            <div className="empty-state glass-panel">
              <HelpCircle className="empty-icon" />
              <h3>No Summaries Found</h3>
              <p>Any ingested classes will automatically show up here after AI processing.</p>
            </div>
          ) : (
            <div className="summaries-grid">
              {filteredSummaries.map((s) => (
                <div 
                  key={s.id} 
                  className="summary-card glass-panel"
                  onClick={() => setSelectedSummary(s)}
                >
                  <div className="card-header">
                    <span className="card-subject">{s.subject}</span>
                    <span className="card-date">
                      <Calendar size={14} style={{ marginRight: '0.25rem', verticalAlign: 'middle' }} />
                      {s.timestamp.split(' ')[0]}
                    </span>
                  </div>
                  <p className="card-body">{s.summary}</p>
                  <div className="card-topics">
                    {s.topics.slice(0, 3).map((t, idx) => (
                      <span key={idx} className="topic-badge">{t}</span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Detailed Summary Modal */}
          {selectedSummary && (
            <div className="modal-overlay" onClick={() => setSelectedSummary(null)}>
              <div className="modal-content glass-panel" onClick={(e) => e.stopPropagation()}>
                <button className="btn-close" onClick={() => setSelectedSummary(null)}>
                  <X size={20} />
                </button>
                
                <h2 className="detail-subject">{selectedSummary.subject}</h2>
                <div className="detail-date">
                  <Calendar size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
                  Recorded on {selectedSummary.timestamp}
                </div>

                <div className="detail-section">
                  <h3>
                    <FileText size={18} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
                    AI Concept Abstract
                  </h3>
                  <p className="detail-text">{selectedSummary.summary}</p>
                </div>

                <div className="detail-section">
                  <h3>
                    <Compass size={18} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
                    Topics Map
                  </h3>
                  <div className="topics-list">
                    {selectedSummary.topics.map((t, idx) => (
                      <span key={idx} className="topic-badge">{t}</span>
                    ))}
                  </div>
                </div>

                <div className="detail-section">
                  <h3>
                    <ListChecks size={18} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
                    Key Core Takeaways
                  </h3>
                  <ul className="points-list">
                    {selectedSummary.key_points.map((p, idx) => (
                      <li key={idx} className="point-item">
                        <span className="point-bullet" />
                        <span>{p}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )}
        </main>
      )}
    </div>
  );
}

export default App;
