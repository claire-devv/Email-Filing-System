import { useState, useEffect, useRef } from 'react'
import { searchEntities } from '../api/client'

// Typeahead for the master client folder. Searches entities server-side (capped) so it
// scales past a giant dropdown, and allows free text so a reviewer can file to a brand-new
// entity (the backend's Correct flow permits new entity names).
export default function Combobox({ value, onChange, placeholder = 'Search client folder…' }) {
  const [query, setQuery] = useState(value || '')
  const [open, setOpen] = useState(false)
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const boxRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const id = setTimeout(async () => {
      try {
        setLoading(true)
        const data = await searchEntities(query, 20)
        setResults(Array.isArray(data) ? data : [])
        setActiveIndex(-1)
      } catch {
        setResults([])
      } finally {
        setLoading(false)
      }
    }, 200)
    return () => clearTimeout(id)
  }, [query, open])

  useEffect(() => {
    const onDocClick = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  const choose = (name) => {
    setQuery(name)
    onChange(name, true) // true = picked an existing (known) entity
    setOpen(false)
  }

  const trimmed = query.trim()
  const exactMatch = results.some((r) => r.entity_name.toLowerCase() === trimmed.toLowerCase())
  const showCreate = !!trimmed && !exactMatch

  const createNew = () => {
    setQuery(trimmed)
    onChange(trimmed, false) // false = new entity; the backend creates the folder on file
    setOpen(false)
  }

  const onKeyDown = (e) => {
    if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      setOpen(true)
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => (i + 1 < results.length ? i + 1 : 0))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => (i > 0 ? i - 1 : results.length - 1))
    } else if (e.key === 'Enter') {
      if (open && activeIndex >= 0 && results[activeIndex]) {
        e.preventDefault()
        choose(results[activeIndex].entity_name)
      } else if (open && showCreate) {
        e.preventDefault()
        createNew()
      }
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="combobox" ref={boxRef}>
      <input
        className="form-control"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        value={query}
        placeholder={placeholder}
        onChange={(e) => {
          setQuery(e.target.value)
          onChange(e.target.value, false) // false = free text (may be a new entity)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {open && (
        <div className="combobox-menu" role="listbox">
          {loading && <div className="combobox-empty">Searching…</div>}
          {!loading && results.length === 0 && !showCreate && (
            <div className="combobox-empty">Type a client folder name…</div>
          )}
          {results.map((entity, index) => (
            <button
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              key={entity.id}
              className={`combobox-option${index === activeIndex ? ' is-active' : ''}`}
              onClick={() => choose(entity.entity_name)}
            >
              {entity.entity_name}
            </button>
          ))}
          {!loading && showCreate && (
            <button type="button" className="combobox-option combobox-create" onClick={createNew}>
              + Create “{trimmed}”
            </button>
          )}
        </div>
      )}
    </div>

  )
}
