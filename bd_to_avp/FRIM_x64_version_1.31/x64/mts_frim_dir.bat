@echo off

for %%F in ( *.m2ts *.mts *.ts ) do (
  call mts_frim_one.bat %%F L
  call mts_frim_one.bat %%F R
)
