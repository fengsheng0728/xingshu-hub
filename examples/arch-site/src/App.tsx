import { Routes, Route } from 'react-router'
import Home from './pages/Home'
import HubStatusBanner from './components/HubStatusBanner'

export default function App() {
  return (
    <>
      <HubStatusBanner />
      <Routes>
        <Route path="/" element={<Home />} />
      </Routes>
    </>
  )
}
