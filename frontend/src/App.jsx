import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { getTokens } from './api'
import Login from './pages/Login'
import Setup from './pages/Setup'
import Library from './pages/Library'
import DocumentView from './pages/DocumentView'
import Settings from './pages/Settings'

function RequireAuth({ children }) {
  if (!getTokens()) return <Navigate to="/login" replace />
  return children
}

export default function App() {
  const [, setAuthTick] = useState(0)
  const navigate = useNavigate()

  useEffect(() => {
    const onChange = () => {
      setAuthTick((n) => n + 1)
      if (!getTokens()) navigate('/login')
    }
    window.addEventListener('auth-changed', onChange)
    return () => window.removeEventListener('auth-changed', onChange)
  }, [navigate])

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/setup" element={<Setup />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Library />
          </RequireAuth>
        }
      />
      <Route
        path="/doc/:id"
        element={
          <RequireAuth>
            <DocumentView />
          </RequireAuth>
        }
      />
      <Route
        path="/settings"
        element={
          <RequireAuth>
            <Settings />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
