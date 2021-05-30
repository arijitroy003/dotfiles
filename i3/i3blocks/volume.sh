pavu_status=$(pulseaudio-ctl full-status)
pavu_status=($pavu_status)

volume=${pavu_status[0]}
muted=${pavu_status[1]}

if [ "$muted" == "yes" ]; then
  echo '<span foreground="#Cd3f45"> </span>'
elif [ $volume == 0 ]; then
  echo '<span foreground="#0000ff">' $volume'</span>'
elif [ "$volume" -gt 0 ] && [ $volume -le 40 ]; then
  echo '<span foreground="#ffff00">' $volume'</span>'
else
  echo '<span foreground="#00ff00">' $volume'</span>'
fi
