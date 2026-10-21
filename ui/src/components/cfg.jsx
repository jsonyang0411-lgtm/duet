// Form controls bound to one key of the shared settings draft (store/config.jsx).
import { useId } from 'react'
import { useConfig, useField } from '../store/config'
import { NUMBER_RULES, ruleText } from '../lib/schema'
import { ChipsInput, Field, NumberInput, Select, SecretInput, Seg, TextArea, TextInput, ToggleRow } from './ui'

export function CfgText({ k, label, envKey, hint, warn, mono, placeholder, className }) {
  const [v, set, err] = useField(k)
  const id = useId()
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} warn={warn} id={id} className={className}>
      <TextInput id={id} value={v} onChange={set} mono={mono} placeholder={placeholder} aria-invalid={err ? 'true' : undefined} />
    </Field>
  )
}

export function CfgTextArea({ k, label, envKey, hint, rows, className, placeholder }) {
  const [v, set, err] = useField(k)
  const id = useId()
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} id={id} className={className}>
      <TextArea id={id} value={v} onChange={set} rows={rows} placeholder={placeholder} />
    </Field>
  )
}

export function CfgNumber({ k, label, envKey, hint, suffix, className }) {
  const [v, set, err] = useField(k)
  const id = useId()
  const rule = NUMBER_RULES[k]
  const range = ruleText(rule)
  return (
    <Field
      label={label} envKey={envKey} error={err} id={id} className={className}
      hint={[hint, range && `范围：${range}`].filter(Boolean).join(' · ')}
    >
      <NumberInput id={id} value={v} onChange={set} invalid={!!err} suffix={suffix} />
    </Field>
  )
}

export function CfgSelect({ k, label, envKey, hint, options, className }) {
  const [v, set, err] = useField(k)
  const id = useId()
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} id={id} className={className}>
      <Select id={id} value={v} onChange={set} options={options} />
    </Field>
  )
}

export function CfgSeg({ k, label, envKey, hint, options, className }) {
  const [v, set, err] = useField(k)
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} className={className}>
      <div><Seg value={v} onChange={set} options={options} label={label} /></div>
    </Field>
  )
}

export function CfgSecret({ k, label, envKey, hint, optional, className }) {
  const [v, set, err] = useField(k)
  const { base } = useConfig()
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} className={className}>
      <SecretInput value={v} onChange={set} saved={base?.[k]} optional={optional} />
    </Field>
  )
}

export function CfgChips({ k, label, envKey, hint, placeholder, minItems, allLabel, className }) {
  const [v, set, err] = useField(k)
  return (
    <Field label={label} envKey={envKey} hint={hint} error={err} className={className}>
      <ChipsInput value={v} onChange={set} placeholder={placeholder} minItems={minItems} allLabel={allLabel} />
    </Field>
  )
}

export function CfgToggle({ k, title, desc, envKey }) {
  const [v, set] = useField(k)
  return <ToggleRow title={title} desc={desc} envKey={envKey} checked={!!v} onChange={set} />
}
